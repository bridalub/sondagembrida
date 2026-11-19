#!/usr/bin/env bash
# Publica o sondagem-brida no Caddy compartilhado: acrescenta SÓ o bloco do domínio novo.
#   sudo bash deploy/publicar_dominio.sh sondagem.bridalub.net.br --teste   # mostra a diferença; não altera nada
#   sudo bash deploy/publicar_dominio.sh sondagem.bridalub.net.br           # mostra, pede SIM, aplica e recarrega
# Não reinicia o Caddy (só reload), não mexe em blocos de outros projetos, não restaura backup sozinho.
set -euo pipefail

DOMINIO="${1:-}"; MODO="${2:-}"
BASE=/opt/apps/sondagem-brida
CADDYFILE=/etc/caddy/Caddyfile
PORTA=8050
IP_SERVIDOR=179.199.142.63
INICIO="# >>> sondagem-brida (bloco único deste projeto; remover com deploy/despublicar_dominio.sh)"
FIM="# <<< sondagem-brida"
COMPOSE=(docker compose -p sondagem-brida -f "$BASE/deploy/compose.yaml" --env-file "$BASE/.env")

falha(){ echo "ERRO: $*" >&2; exit 1; }
como_deploy(){ runuser -u deploy -- "$@"; }

[[ "$DOMINIO" =~ ^[a-z0-9]([a-z0-9.-]*[a-z0-9])?\.[a-z]{2,}$ ]] || falha "informe o domínio, ex.: sudo bash $0 sondagem.bridalub.net.br --teste"
[[ $EUID -eq 0 ]] || falha "rode com sudo (o Caddyfile pertence ao root)."
[[ -f "$CADDYFILE" ]] || falha "$CADDYFILE não encontrado."

echo "== 1. Conferências (nada é alterado aqui)"
IP_DNS=$(getent ahostsv4 "$DOMINIO" | awk 'NR==1{print $1}' || true)
echo "   servidor=$IP_SERVIDOR  dns($DOMINIO)=${IP_DNS:-sem registro}"
DNS_OK=0; [[ "$IP_DNS" == "$IP_SERVIDOR" ]] && DNS_OK=1
grep -qE "^[[:space:]]*$(sed 's/\./\\./g' <<<"$DOMINIO")([[:space:],{]|$)" "$CADDYFILE" && falha "$DOMINIO já existe no Caddyfile — nada foi alterado."
grep -qF "$INICIO" "$CADDYFILE" && falha "o bloco do sondagem-brida já está no Caddyfile — nada foi alterado."
[[ "$(docker inspect -f '{{.State.Health.Status}}' sondagem-brida-web 2>/dev/null)" == healthy ]] || falha "o container sondagem-brida-web não está healthy."
curl -s -o /dev/null --max-time 10 -H "Host: localhost" -w '%{http_code}' "http://127.0.0.1:$PORTA/entrar/" | grep -q 200 \
  || falha "o sistema não responde em 127.0.0.1:$PORTA."
echo "   container saudável e respondendo em 127.0.0.1:$PORTA."

NOVO=$(mktemp); trap 'rm -f "$NOVO"' EXIT
cat "$CADDYFILE" > "$NOVO"
[[ -z "$(tail -c1 "$NOVO")" ]] || echo >> "$NOVO"
cat >> "$NOVO" <<BLOCO
$INICIO
$DOMINIO {
	encode gzip
	request_body {
		max_size 20MB
	}
	reverse_proxy 127.0.0.1:$PORTA
}
$FIM
BLOCO

echo "== 2. Diferença exata que será aplicada no $CADDYFILE"
diff -u "$CADDYFILE" "$NOVO" || true
caddy validate --config "$NOVO" --adapter caddyfile >/dev/null 2>&1 \
  || { caddy validate --config "$NOVO" --adapter caddyfile; falha "o Caddyfile resultante é inválido — nada foi alterado."; }
echo "   Caddyfile resultante válido."

if [[ "$MODO" == "--teste" ]]; then
  [[ $DNS_OK == 1 ]] && echo "   DNS ok." || echo "   AVISO: o DNS ainda não aponta para $IP_SERVIDOR (necessário antes de publicar)."
  echo "== Teste concluído. Nada foi alterado."; exit 0
fi
[[ $DNS_OK == 1 ]] || falha "o DNS de $DOMINIO ainda não aponta para $IP_SERVIDOR. Crie o registro A e rode de novo."

read -r -p "Aplicar exatamente a diferença acima e recarregar o Caddy (reload, sem restart)? Digite SIM: " RESP
[[ "$RESP" == "SIM" ]] || falha "cancelado — nada foi alterado."

CARIMBO=$(date +%Y%m%d%H%M%S)
echo "== 3. .env do sondagem-brida (backup: .env.antes_dominio_$CARIMBO)"
como_deploy cp -p "$BASE/.env" "$BASE/.env.antes_dominio_$CARIMBO"
definir(){ if grep -q "^$1=" "$BASE/.env"; then como_deploy sed -i "s|^$1=.*|$1=$2|" "$BASE/.env"; else echo "$1=$2" | como_deploy tee -a "$BASE/.env" >/dev/null; fi; }
definir DJANGO_ALLOWED_HOSTS "$DOMINIO,localhost,127.0.0.1"
definir DJANGO_CSRF_TRUSTED_ORIGINS "https://$DOMINIO"
definir DJANGO_HTTPS 1
como_deploy "${COMPOSE[@]}" up -d --no-deps --force-recreate web
for _ in $(seq 1 30); do
  [[ "$(docker inspect -f '{{.State.Health.Status}}' sondagem-brida-web)" == healthy ]] && break; sleep 3
done
[[ "$(docker inspect -f '{{.State.Health.Status}}' sondagem-brida-web)" == healthy ]] || falha "o web não voltou healthy; o Caddy NÃO foi alterado. Volte o .env do backup."

echo "== 4. Caddy (backup só para recuperação assistida: $CADDYFILE.antes_sondagem_$CARIMBO)"
cp -p "$CADDYFILE" "$CADDYFILE.antes_sondagem_$CARIMBO"
cat "$NOVO" > "$CADDYFILE"      # mesmo arquivo, mesmo dono e permissões
systemctl reload caddy || falha "o reload falhou. O Caddy segue com a configuração anterior em memória. Remova SÓ o bloco do sondagem com deploy/despublicar_dominio.sh (não restaure o arquivo inteiro)."

echo "== 5. Conferência"
sleep 5
curl -s -o /dev/null --max-time 20 -w "   https://$DOMINIO/entrar/ -> HTTP %{http_code}\n" "https://$DOMINIO/entrar/" || true
echo "Pronto. Para retirar: sudo bash $BASE/deploy/despublicar_dominio.sh"
