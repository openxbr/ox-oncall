#!/bin/sh
# Gera os 3 env files de segredo da stack de teste (db.env, oncall.env, grafana.env).
# Não sobrescreve: se algum já existe, para. A senha do Postgres é a mesma nos três.
set -eu
cd "$(dirname "$0")"
for f in db.env oncall.env grafana.env; do
  [ ! -e "$f" ] || { echo "$f já existe; nada feito" >&2; exit 1; }
done
umask 077
rnd() { head -c 64 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | cut -c1-"$1"; }
DBPASS=$(rnd 32)
printf 'POSTGRES_PASSWORD=%s\n' "$DBPASS" > db.env
cat > oncall.env <<EOT
SECRET_KEY=$(rnd 50)
MIRAGE_SECRET_KEY=$(rnd 50)
MIRAGE_CIPHER_IV=$(rnd 16)
DATABASE_PASSWORD=$DBPASS
# Bot de TESTE (BotFather, Group Privacy desligado). Vazio até existir.
TELEGRAM_TOKEN=
EOT
cat > grafana.env <<EOT
GF_SECURITY_ADMIN_PASSWORD=$(rnd 24)
GF_DATABASE_PASSWORD=$DBPASS
EOT
ls -l db.env oncall.env grafana.env
