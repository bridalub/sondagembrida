#!/usr/bin/env bash
# Preenche as chaves de API e do Salesforce no .env do sondagem-brida, sem editor e sem exibir os valores.
#   bash deploy/definir_chaves.sh          (como root ou deploy, a partir de /opt/apps/sondagem-brida)
# Enter vazio = mantém o valor atual. Faz backup do .env e recria só o container sondagem-brida-web.
set -euo pipefail
BASE=/opt/apps/sondagem-brida
ENV="$BASE/.env"
COMPOSE=(docker compose -p sondagem-brida -f "$BASE/deploy/compose.yaml" --env-file "$ENV")
falha(){ echo "ERRO: $*" >&2; exit 1; }
[[ -f "$ENV" ]] || falha "$ENV não encontrado."

CAMPOS=(OPENAI_API_KEY SERPAPI_API_KEY SF_USERNAME SF_PASSWORD SF_SECURITY_TOKEN SF_INSTANCE_URL)
declare -A NOVO
echo "Cole cada valor e aperte Enter (nada aparece na tela). Enter vazio mantém o atual."
for k in "${CAMPOS[@]}"; do
  atual=$(grep -m1 "^$k=" "$ENV" | cut -d= -f2- || true)
  estado=$([[ -n "$atual" ]] && echo "preenchido" || echo "vazio")
  if [[ "$k" == "SF_USERNAME" || "$k" == "SF_INSTANCE_URL" ]]; then
    read -r -p "$k [$estado]: " v
  else
    read -r -s -p "$k [$estado]: " v; echo
  fi
  v="${v//$'\r'/}"; v="${v#"${v%%[![:space:]]*}"}"; v="${v%"${v##*[![:space:]]}"}"   # tira espaços e CR
  [[ "$v" == *"|"* ]] && falha "o valor de $k contém '|', não suportado por este script. Nada foi gravado."
  if [[ "$k" == "SF_INSTANCE_URL" && -n "$v" && "$v" != https://* ]]; then
    [[ "$v" == //* ]] && v="https:$v" || v="https://$v"
    echo "   (ajustado para começar com https://)"
  fi
  [[ -n "$v" ]] && NOVO[$k]="$v"
done
[[ ${#NOVO[@]} -gt 0 ]] || { echo "Nenhum valor informado. Nada foi alterado."; exit 0; }

CARIMBO=$(date +%Y%m%d%H%M%S)
cp -p "$ENV" "$ENV.antes_chaves_$CARIMBO"
TMP=$(mktemp); trap 'rm -f "$TMP"' EXIT
cp "$ENV" "$TMP"
for k in "${!NOVO[@]}"; do
  if grep -q "^$k=" "$TMP"; then
    K="$k" V="${NOVO[$k]}" awk 'BEGIN{FS=OFS="="} $1==ENVIRON["K"]{print ENVIRON["K"] "=" ENVIRON["V"]; next} {print}' "$TMP" > "$TMP.2"       && cat "$TMP.2" > "$TMP" && rm -f "$TMP.2"
  else
    printf '%s=%s\n' "$k" "${NOVO[$k]}" >> "$TMP"
  fi
done
cat "$TMP" > "$ENV"     # mesmo arquivo: dono deploy e permissão 600 preservados
echo "Gravado (backup: $(basename "$ENV").antes_chaves_$CARIMBO). Campos alterados: ${!NOVO[*]}"

"${COMPOSE[@]}" up -d --no-deps --force-recreate web
for _ in $(seq 1 30); do [[ "$(docker inspect -f '{{.State.Health.Status}}' sondagem-brida-web)" == healthy ]] && break; sleep 3; done
echo "web: $(docker inspect -f '{{.State.Health.Status}}' sondagem-brida-web)"
echo "Worker e agendador continuam desligados (Fase C)."
