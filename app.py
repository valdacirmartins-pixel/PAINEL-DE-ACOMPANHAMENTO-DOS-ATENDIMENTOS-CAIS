            [
                html.I(className="fa-solid fa-chart-line me-2"),
                html.Strong("Resumo do recorte: "),
                (
                    f"{instrumentos} instrumento(s), {convenentes} "
                    f"convenente(s)/OSC(s) e {formatar_percentual_brl(percentual)} "
                    f"do valor global desembolsado. Última atualização "
                    f"informada na planilha: {ultima_atualizacao}."
                ),
            ],
            color="primary",
            className="rounded-4 mb-0",
        )

    tabela = filtrada.sort_values("Valor Global", ascending=False).copy()
    for coluna in [
        "Valor Global", "Valor Desembolsado", "Valor a Desembolsar",
    ]:
        tabela[coluna] = tabela[coluna].map(formatar_moeda_brl)
    tabela["Percentual Desembolsado"] = tabela[
        "Percentual Desembolsado"
    ].map(formatar_percentual_brl)
    colunas_tabela = [
        "Convenente / OSC",
        "Número do Instrumento",
        "Número do Processo",
        "Região",
        "UF",
        "Tipo",
        "Subtipo",
        "Tema",
        "Status",
        "Valor Global",
        "Valor Desembolsado",
        "Valor a Desembolsar",
        "Percentual Desembolsado",
        "Responsável",
        "Início",
        "Término",
        "Atualização",
        "Objeto",
        "Observação",
    ]

    return (
        formatar_moeda_brl(global_total),
        formatar_moeda_brl(desembolsado),
        formatar_moeda_brl(restante),
        formatar_percentual_brl(percentual),
        str(instrumentos),
        str(convenentes),
        (
            formatar_moeda_brl(desembolso_periodo)
            if historico_suficiente
            else "Aguardando histórico"
        ),
        resumo,
        criar_grafico_regiao_transferencias(
            filtrada,
            tema_selecionado=tema,
        ),
        criar_grafico_status_transferencias(
            filtrada,
            tema_selecionado=tema,
        ),
        criar_grafico_convenentes_transferencias(
            filtrada,
            modo=modo_grafico,
            tema_selecionado=tema,
        ),
        tabela[colunas_tabela].fillna("").to_dict("records"),
    )


app.clientside_callback(
    """
    function(n_clicks) {
        if (!n_clicks) {
            return window.dash_clientside.no_update;
        }
        const redimensionarGraficos = function() {
            window.dispatchEvent(new Event('resize'));
            if (window.Plotly) {
                document.querySelectorAll(
                    '.relatorio-impressao .js-plotly-plot'
                ).forEach(function(grafico) {
                    try {
                        window.Plotly.Plots.resize(grafico);
                    } catch (erro) {
                        // O redimensionamento global continua válido.
                    }
                });
            }
        };
        const finalizarImpressao = function() {
            document.body.classList.remove('modo-impressao');
            window.setTimeout(redimensionarGraficos, 100);
        };
        document.body.classList.add('modo-impressao');
        window.addEventListener(
            'afterprint',
            finalizarImpressao,
            {once: true}
        );
        window.setTimeout(function() {
            redimensionarGraficos();
            window.setTimeout(function() {
                window.print();
            }, 400);
        }, 600);
        return "";
    }
    """,
    Output("saida-impressao-relatorio", "children"),
    Input("botao-imprimir-dashboard", "n_clicks"),
    prevent_initial_call=True,
)


# ============================================================
# EXECUÇÃO
# ============================================================

if __name__ == "__main__":
    app.run(
        debug=True,
        host="127.0.0.1",
        port=8050,
