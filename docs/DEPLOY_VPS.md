# Subir para a VPS (Linux)

Roteiro para Ubuntu 22.04/24.04 com Python 3.12 e PostgreSQL 16. Tudo roda na mesma máquina:

| Serviço | O que faz | Arquivo |
|---|---|---|
| `brida-web` | o site (Gunicorn em 127.0.0.1:8000, atrás do Nginx com HTTPS) | `deploy/systemd/brida-web.service` |
| `brida-worker` | fila de buscas: executa cada "Buscar novos leads" e as continuações automáticas | `deploy/systemd/brida-worker.service` |
| `brida-sincronizar.timer` | **de hora em hora**: clientes, contatos, leads e notas fiscais dos últimos 15 dias do Salesforce (somente leitura) | `deploy/systemd/brida-sincronizar.*` |
| Nginx | HTTPS e arquivos estáticos | `deploy/nginx-brida.conf` |

Com a sincronização de hora em hora ligada, o botão **Buscar** não lê o Salesforce: usa a carteira sincronizada
há menos de 70 minutos (`PROSPECCAO_SYNC_JANELA_MIN`). Se a automação parar, a busca volta a sincronizar sozinha.

## 1. Pacotes e usuário

```bash
sudo apt update && sudo apt install -y python3.12 python3.12-venv postgresql-16 nginx certbot python3-certbot-nginx
sudo useradd --system --create-home --home-dir /opt/brida-prospeccao brida
```

## 2. Banco

```bash
sudo -u postgres psql -c "CREATE USER carteira WITH PASSWORD '<senha forte>';"
sudo -u postgres psql -c "CREATE DATABASE carteira OWNER carteira;"
```

**Levar os dados do computador atual** (carteira, pesquisas, leads, contatos, notas e a base da Receita):

```powershell
# no computador atual (PowerShell); o banco local roda no Docker
docker exec brida-prospeccao-pg pg_dump -U carteira -d carteira -Fc -f /tmp/carteira.dump
docker cp brida-prospeccao-pg:/tmp/carteira.dump .\carteira.dump
```

```bash
# na VPS, depois de copiar carteira.dump (scp/WinSCP)
sudo -u postgres pg_restore -d carteira --no-owner --role=carteira carteira.dump
```

A base da Receita (92 municípios) é grande. Se preferir não copiar, importe direto na VPS (passo 6).

## 3. Código e dependências

O código vem do GitHub (`bridalub/sondagembrida`). Segredos (`.env`), banco, anexos e `Excel.py` não estão no
repositório: o `.env` é criado na VPS (passo 4).

```bash
sudo -u brida git clone https://github.com/bridalub/sondagembrida.git /tmp/sondagembrida
sudo -u brida cp -a /tmp/sondagembrida/. /opt/brida-prospeccao/ && rm -rf /tmp/sondagembrida
cd /opt/brida-prospeccao
sudo -u brida python3.12 -m venv .venv
sudo -u brida .venv/bin/pip install -r requirements.txt
```

## 4. Configuração (`/opt/brida-prospeccao/.env`, dono `brida`, permissão 600)

Mesmas variáveis do `.env` atual, com estes valores de servidor:

```
DJANGO_DEBUG=0
DJANGO_SECRET_KEY=<nova chave longa e aleatória>
DJANGO_ALLOWED_HOSTS=prospeccao.exemplo.com.br
DJANGO_CSRF_TRUSTED_ORIGINS=https://prospeccao.exemplo.com.br
DJANGO_HTTPS=1
POSTGRES_HOST=localhost
POSTGRES_PORT=5432
POSTGRES_DB=carteira
POSTGRES_USER=carteira
POSTGRES_PASSWORD=<senha do passo 2>
PROSPECCAO_DISPARAR_WORKER=0
```

Mais as credenciais que já existem hoje: `SF_USERNAME`, `SF_PASSWORD`, `SF_SECURITY_TOKEN`, `SF_INSTANCE_URL`,
`SF_SITUACOES_CLIENTE`, `OPENAI_API_KEY`, `PROSPECCAO_IA_*`, `PROSPECCAO_BUSCA_PROVEDOR`, `SERPAPI_API_KEY`,
`PROSPECCAO_TETO_GLOBAL_USD`. **Tudo num arquivo só** (`.env`); na VPS não use `env.env`.

```bash
sudo chown brida:brida .env && sudo chmod 600 .env
sudo -u brida .venv/bin/python manage.py migrate
sudo -u brida .venv/bin/python manage.py collectstatic --noinput
sudo -u brida .venv/bin/python manage.py check --deploy
```

## 5. Serviços

```bash
sudo cp deploy/systemd/brida-*.service deploy/systemd/brida-*.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now brida-web brida-worker brida-sincronizar.timer
sudo systemctl start brida-sincronizar      # primeira sincronização agora
systemctl list-timers brida-sincronizar.timer
journalctl -u brida-sincronizar -n 30      # resultado da última sincronização
```

Nginx e HTTPS:

```bash
sudo cp deploy/nginx-brida.conf /etc/nginx/sites-available/brida   # troque o domínio no arquivo
sudo ln -s /etc/nginx/sites-available/brida /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
sudo certbot --nginx -d prospeccao.exemplo.com.br
```

Sem systemd (alternativa com cron, usuário `brida`):

```
5 * * * * cd /opt/brida-prospeccao && .venv/bin/python manage.py sincronizar_automatico >> logs/sincronizar.log 2>&1
```

## 6. Base da Receita direto na VPS (se não veio no dump)

```bash
sudo -u brida .venv/bin/python manage.py importar_receita --competencia 2026-11 --uf SP \
     --municipios-arquivo receita/municipios_atuacao.txt --apagar-arquivos
```

Cerca de 7 GB de download; roda em fluxo e retoma se cair. Refaça todo mês com a competência nova.

## Atualizar a VPS depois de um novo push

```bash
cd /opt/brida-prospeccao
sudo -u brida git pull --ff-only
sudo -u brida .venv/bin/pip install -r requirements.txt
sudo -u brida .venv/bin/python manage.py migrate
sudo -u brida .venv/bin/python manage.py collectstatic --noinput
sudo systemctl restart brida-web brida-worker
```

Quando o repositório virar **privado**, a VPS precisa de acesso de leitura: crie uma *deploy key* (chave SSH só de
leitura) em GitHub → repositório → Settings → Deploy keys e troque o remoto para
`git@github.com:bridalub/sondagembrida.git`.

## 7. Conferência depois de subir

- `https://<domínio>/` abre o login; entrar com o usuário de sempre (veio no dump).
- `journalctl -u brida-sincronizar -n 30` mostra "Sincronização automática concluída".
- Uma busca de teste: Pesquisas → Buscar novos leads → a execução anda sozinha (worker) e continua sozinha nos limites.
- Painel de captação → "Ver simulação" confirma gráficos e estilos (arquivos estáticos).
- Backups: ver `docs/OPERACAO_DISCO_E_BACKUP.md` (agendar `pg_dump` diário na VPS).

## Orçamentos anexados (arquivos)
- Os arquivos ficam em `DJANGO_MEDIA_ROOT` (padrão: pasta `media/` do projeto). Na VPS, aponte para um disco com backup, por exemplo `DJANGO_MEDIA_ROOT=/srv/brida/media`, e dê permissão de escrita ao usuário do gunicorn.
- Não sirva essa pasta pelo nginx: os arquivos só abrem pela rota protegida `/abordagens/<id>/orcamento/` (quem registrou ou ADMIN).
- Tamanho máximo por arquivo: `ANEXO_MAX_MB` (padrão 10). No nginx, deixe `client_max_body_size 12m;`.
- As notas fiscais dos CNPJs confirmados chegam pela sincronização de hora em hora (`brida-sincronizar.timer` → `sincronizar_automatico`, que inclui `--compras`).
