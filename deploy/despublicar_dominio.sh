#!/usr/bin/env bash
# Retira do Caddy compartilhado SOMENTE o bloco do sondagem-brida (entre os marcadores), preservando
# qualquer alteração feita depois por outros projetos. NÃO restaura backup do arquivo inteiro.
#   sudo bash deploy/despublicar_dominio.sh --teste   # mostra a diferença; não altera nada
#   sudo bash deploy/despublicar_dominio.sh           # mostra, pede SIM, aplica e recarrega (reload)
set -euo pipefail

MODO="${1:-}"
CADDYFILE=/etc/caddy/Caddyfile
INICIO="# >>> sondagem-brida (bloco único deste projeto; remover com deploy/despublicar_dominio.sh)"
FIM="# <<< sondagem-brida"

falha(){ echo "ERRO: $*" >&2; exit 1; }
[[ $EUID -eq 0 ]] || falha "rode com sudo (o Caddyfile pertence ao root)."
n_ini=$(grep -cxF "$INICIO" "$CADDYFILE" || true)
n_fim=$(grep -cxF "$FIM" "$CADDYFILE" || true)
[[ "$n_ini" == 1 && "$n_fim" == 1 ]] || falha "marcadores do sondagem-brida não encontrados exatamente uma vez (início=$n_ini, fim=$n_fim). Nada foi alterado; faça a remoção assistida."

NOVO=$(mktemp); trap 'rm -f "$NOVO"' EXIT
awk -v ini="$INICIO" -v fim="$FIM" '$0==ini{dentro=1; next} dentro && $0==fim{dentro=0; next} !dentro' "$CADDYFILE" > "$NOVO"

echo "== Diferença exata (só linhas do sondagem-brida devem sair)"
diff -u "$CADDYFILE" "$NOVO" || true
caddy validate --config "$NOVO" --adapter caddyfile >/dev/null 2>&1 \
  || { caddy validate --config "$NOVO" --adapter caddyfile; falha "resultado inválido — nada foi alterado."; }
[[ "$MODO" == "--teste" ]] && { echo "== Teste concluído. Nada foi alterado."; exit 0; }

read -r -p "Remover exatamente as linhas acima e recarregar o Caddy (reload)? Digite SIM: " RESP
[[ "$RESP" == "SIM" ]] || falha "cancelado — nada foi alterado."
CARIMBO=$(date +%Y%m%d%H%M%S)
cp -p "$CADDYFILE" "$CADDYFILE.antes_despublicar_sondagem_$CARIMBO"
cat "$NOVO" > "$CADDYFILE"
systemctl reload caddy || falha "o reload falhou; o Caddy segue com a configuração anterior em memória. Recuperação assistida."
echo "Bloco do sondagem-brida removido. Os containers do sondagem-brida continuam como estavam."
