# Inteligência comercial e prospecção – BRIDA (Django + PostgreSQL)

**O que faz:** descobre empresas na internet que podem comprar os nossos produtos, identifica o CNPJ, cruza
com a carteira do Salesforce para não apresentar clientes como novidade e organiza as oportunidades para
abordagem, com histórico de contatos e retornos. A análise e a reativação da carteira são funções auxiliares.

- Arquitetura, regras e fontes: [docs/PROSPECCAO.md](docs/PROSPECCAO.md)
- Diagnóstico da base real: [docs/DIAGNOSTICO_DADOS_REAIS.md](docs/DIAGNOSTICO_DADOS_REAIS.md)
- Análise da automação original: [docs/ANALISE_AUTOMACAO.md](docs/ANALISE_AUTOMACAO.md)

## Instalação

```powershell
pip install -r requirements.txt
# PostgreSQL local de desenvolvimento (Docker), acessível só em 127.0.0.1:
docker run -d --name brida-prospeccao-pg -e POSTGRES_DB=carteira -e POSTGRES_USER=carteira `
  -e POSTGRES_PASSWORD=<senha> -p 127.0.0.1:55432:5432 -v brida_prospeccao_pg:/var/lib/postgresql/data postgres:16-alpine
copy .env.example .env      # preencha DJANGO_SECRET_KEY e POSTGRES_* (porta 55432)
python manage.py migrate    # cria tabelas + fontes, catálogo e regras iniciais
python manage.py createsuperuser
python manage.py runserver
```

As credenciais do Salesforce são lidas do `env.env` da automação (ou do `.env`) e nunca aparecem em tela ou em
log.

## Comandos

| Comando | Para quê |
|---|---|
| `python manage.py sync_salesforce` | Leitura do Salesforce: clientes (com Bloqueados), contatos, leads BRIDA e notas. Reavalia as oportunidades |
| `python manage.py sync_salesforce --clientes --leads` | Só a carteira e os leads (cruzamento rápido) |
| `python manage.py importar_receita --competencia 2026-10 --uf SP --municipio GUARULHOS` | Base pública de CNPJ (em fluxo, com retomada) |
| `python manage.py executar_pesquisa <id>` | Executa uma pesquisa agora, no terminal |
| `python manage.py executar_pesquisa <id> --csv arquivo.csv` | Importa candidatos de um CSV (não é mineração) |
| `python manage.py prospeccao_worker [--uma-vez]` | Processa a fila: execuções manuais e, se ativado, as recorrentes |
| `python manage.py recalcular_indicadores` | Recalcula os indicadores de compra |
| `$env:DB_ENGINE='sqlite'; python manage.py test carteira prospeccao` | Testes com dados sintéticos |

**Execução recorrente:** fica desligada por padrão. Para ligar:
1. marque *Admin → Configuração de análise → agendamento ativo*;
2. preencha a frequência em cada pesquisa;
3. deixe `prospeccao_worker` rodando ou agende `prospeccao_worker --uma-vez` no Agendador de Tarefas do Windows
   (por exemplo, a cada 15 minutos).

## Telas

| Tela | URL |
|---|---|
| Visão geral da prospecção | `/` |
| Pesquisas (configuração por segmento e região, importação CSV) | `/prospeccao/pesquisas/` |
| Execuções (progresso, resultados, limites, falhas, log) | `/prospeccao/execucoes/` |
| Oportunidades (filtros, responsável, exportação das liberadas) | `/prospeccao/oportunidades/` |
| Empresa (fontes, evidências, canais, pessoas, histórico, decisões) | `/prospeccao/empresas/<id>/` |
| Revisão de candidatos sem identificação ou com conflito | `/prospeccao/revisao/` |
| Abordagem / retornos | `/empresas/<id>/abordagem/`, `/retornos/` |
| Base Receita (cargas, panorama por município) | `/prospeccao/base-receita/` |
| Fontes e limites (sem segredos) | `/prospeccao/fontes/` |
| Carteira (resumo, empresas, reativação, importações) | `/carteira/`, `/empresas/`, `/importacoes/` |
