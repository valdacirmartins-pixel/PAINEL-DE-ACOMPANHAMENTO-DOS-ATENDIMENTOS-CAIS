# Monitoramento Cidadania

Painel em Python/Dash para acompanhamento dos atendimentos registrados no Sistema CAIS.

## Arquivos do projeto

- `app.py`: aplicação principal.
- `requirements.txt`: dependências Python.
- `Procfile`: comando de inicialização.
- `railway.json`: configuração de build e deploy.
- `.python-version`: versão principal do Python usada no deploy.
- `.gitignore`: impede o envio de bases, bancos e arquivos pessoais ao GitHub.

## Executar no computador

```bash
python -m venv .venv
pip install -r requirements.txt
python app.py
```

Depois, acesse `http://127.0.0.1:8050`.

## Publicar no Railway

1. Envie os arquivos deste projeto para a raiz do repositório GitHub.
2. No Railway, selecione esse repositório como fonte do serviço.
3. Crie um Volume persistente e monte-o em `/data`.
4. Em **Variables**, crie `STORAGE_PATH=/data`.
5. Ainda em **Variables**, defina `APP_USERNAME` e `APP_PASSWORD` para proteger o painel.
6. Gere o domínio público do serviço e faça um novo deploy.

O serviço é iniciado pelo Gunicorn com apenas um processo. Isso evita conflitos de escrita no banco SQLite usado pelo histórico de importações.

## Persistência e privacidade

O sistema guarda o banco SQLite, os arquivos importados e as cópias consolidadas dentro de `STORAGE_PATH`. No Railway, essa variável deve apontar para o Volume persistente.

As pastas de dados e os formatos de planilha estão no `.gitignore`. Não envie bases reais do CAIS ao GitHub, mesmo que o repositório seja privado.

Não use bases reais no domínio público antes de definir `APP_USERNAME` e `APP_PASSWORD`. As duas variáveis devem ser configuradas juntas e seus valores não devem ser escritos em nenhum arquivo do repositório.
