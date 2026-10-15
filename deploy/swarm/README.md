# OnCall de teste num Docker Swarm

Prova de conceito do fork `appwrite/grafana-oncall` na **release publicada v1.24.10**
(21/09/2026), sem build de imagem. **Não é produção**: o Grafana desta stack é **próprio**
(o Grafana de produção não recebe plugin de teste), e a integração com o Zabbix usa só objetos
de teste (`../zabbix/`).

## O que sobe

Stack `oncall-teste`, tudo fixado num nó (`ONCALL_NODE`), portas no endereço `ONCALL_HOST`.

| Serviço | Imagem | Porta | Dados |
|---|---|---|---|
| `engine` | `ghcr.io/appwrite/grafana-oncall@sha256:2645…ccf4` (manifest da v1.24.10) | **18080** (host) | — |
| `celery` | idem (worker + beat) | — | — |
| `migrate` | idem (`manage.py migrate`, roda e sai) | — | — |
| `telegram-polling` | idem (long polling, **0 réplica** até ter bot) | — | — |
| `db` | `postgres:18.6@sha256:7493…4336` (bancos `oncall` e `grafana`) | — | `/mnt2/oncall-teste/postgres` |
| `redis` | `redis:8.2.9@sha256:66b7…1b21` (broker + cache, AOF) | — | `/mnt2/oncall-teste/redis` |
| `grafana` | `grafana/grafana:13.2.0@sha256:3fd5…429b` (OSS) | **13000** (host) | `/mnt2/oncall-teste/grafana` |

Arquivos desta pasta:

| Arquivo | Para quê |
|---|---|
| `oncall-teste.stack.yml` | a stack |
| `stack.env.example` | variáveis do deploy, não secretas (`ONCALL_HOST`, `ONCALL_NODE`, `TELEGRAM_POLLING_REPLICAS`) |
| `gerar-env.sh` | gera `db.env`, `oncall.env`, `grafana.env` (segredos, 600, nunca no git) |
| `settings_openx.py` | `settings.helm` + `httpx` em WARNING, montado por bind no container (ver abaixo) |
| `plugin-provisioning.yaml`, `initdb/10-grafana.sql` | aponta o plugin para o engine; cria o banco do Grafana |
| `configurar_poc.py` | roda no manager e configura tudo pela API: plugin, usuários, token, integração, escala, chain, Telegram |

## Por que assim (e o que quebrou no caminho)

- **Digest, não tag.** O `COMPATIBILITY.md` do fork diz que o digest do `release-manifest.json`
  é o artefato autoritativo. O plugin vem do zip da mesma release, com o SHA-256 conferido
  (`08e2d627…4eba`) antes de extrair. Grafana 13.2.0, PostgreSQL 18.6 e Redis 8.2.9 são as
  versões da matriz testada do fork.
- **Um nó só, portas em `mode: host`.** Sem VXLAN entre os serviços e sem ingress mesh.
- **`endpoint_mode: dnsrr` em todos os serviços.** No Swarm da PoC (nós em LXC) o VIP da
  overlay não respondia nem entre containers do mesmo nó: `db` (VIP) dava timeout e `tasks.db`
  (IP da task) conectava; IPVS vazio no netns do container. Sintoma: migration e Grafana com
  "timed out waiting for a database connection" e o engine morto pelo healthcheck.
- **`GRAFANA_PUBLIC_URL`.** O install v2 do fork recusa (401) a URL que o plugin informa se
  ela não for igual a `GRAFANA_API_URL` ou a `GRAFANA_PUBLIC_URL`. O plugin informa o
  `root_url` do Grafana, então `GRAFANA_PUBLIC_URL` = `GF_SERVER_ROOT_URL`.
- **`settings_openx.py` (token fora do log).** O `httpx` do python-telegram-bot loga em INFO a
  URL de cada chamada à Bot API, e essa URL leva o token do bot. O módulo sobe `httpx` e
  `httpcore` para WARNING sem build de imagem.
- **`DJANGO_ALLOW_ASYNC_UNSAFE` só no `telegram-polling`.** Bug do v1.24.10 no long polling: o
  PTB 22.5 chama o handler num event loop async e o `UpdateManager` usa o ORM síncrono, então
  todo update que toca o banco (código de verificação, botão de ack) morre com
  `SynchronousOnlyOperation`. Seguro aqui porque o PTB processa um update por vez
  (`concurrent_updates=1`). A correção de verdade é `sync_to_async` no
  `start_telegram_polling.py`.
- **Telemetria desligada.** `SEND_ANONYMOUS_USAGE_STATS`, heartbeat e relay de notificação
  pelo Grafana Cloud em `False`. Sem o último em `False`, ligação e SMS iriam para a Grafana
  Cloud e não para o provedor configurado.

Exposição: porta em `mode: host` escuta em **todos** os IPs do nó. Para o teste basta; antes de
virar serviço, a stack precisa de TLS num proxy reverso e das portas fechadas.

## Deploy

`<manager>` = um manager do Swarm; `S=/mnt2/oncall-teste/stack`.

### 1. Na estação (clone deste repo)

```bash
scp deploy/swarm/oncall-teste.stack.yml deploy/swarm/gerar-env.sh deploy/swarm/plugin-provisioning.yaml deploy/swarm/settings_openx.py deploy/swarm/stack.env.example deploy/swarm/initdb/10-grafana.sql root@<manager>:/root/
```

### 2. No manager

```bash
S=/mnt2/oncall-teste/stack
mkdir -p $S /mnt2/oncall-teste/postgres /mnt2/oncall-teste/redis /mnt2/oncall-teste/initdb \
  /mnt2/oncall-teste/grafana/data/plugins /mnt2/oncall-teste/grafana/provisioning/plugins
mv /root/oncall-teste.stack.yml /root/gerar-env.sh /root/plugin-provisioning.yaml /root/settings_openx.py $S/
cp /root/stack.env.example $S/stack.env      # editar ONCALL_HOST e ONCALL_NODE
mv /root/10-grafana.sql /mnt2/oncall-teste/initdb/
cp $S/plugin-provisioning.yaml /mnt2/oncall-teste/grafana/provisioning/plugins/grafana-oncall-app.yaml
chmod 644 $S/settings_openx.py
sh $S/gerar-env.sh            # gera db.env, oncall.env, grafana.env (600), não sobrescreve

# Plugin: baixar, conferir o SHA-256 da release, extrair
cd /tmp && curl -fsSLO https://github.com/appwrite/grafana-oncall/releases/download/v1.24.10/grafana-oncall-app-1.24.10.zip
echo "08e2d62742ca584a0b30c26fddf2cd7a428007529ff08fcc618498ba271c4eba  grafana-oncall-app-1.24.10.zip" | sha256sum -c -
python3 -m zipfile -e grafana-oncall-app-1.24.10.zip /mnt2/oncall-teste/grafana/data/plugins/
chmod +x /mnt2/oncall-teste/grafana/data/plugins/grafana-oncall-app/gpx_grafana_oncall_app_linux_*
chown -R 472:0 /mnt2/oncall-teste/grafana/data

cd $S && set -a && . ./stack.env && set +a && docker stack deploy -c oncall-teste.stack.yml oncall-teste
```

### 3. Conferir

```bash
docker stack ps oncall-teste --format '{{.Name}} {{.Node}} {{.CurrentState}} {{.Error}}'
docker service logs --tail 30 oncall-teste_migrate
curl -s -o /dev/null -w '%{http_code}\n' http://<ONCALL_HOST>:18080/health/
curl -s -o /dev/null -w '%{http_code}\n' http://<ONCALL_HOST>:13000/api/health
```

Esperado: `migrate` em `Complete`, os outros `Running`, os dois `curl` com `200`.

### 4. Configurar pela API (do manager)

```bash
ssh root@<manager> python3 - install < deploy/swarm/configurar_poc.py      # liga o plugin ao engine
ssh root@<manager> python3 - usuarios < deploy/swarm/configurar_poc.py     # plantonistas.json -> Grafana -> OnCall
ssh root@<manager> python3 - token < deploy/swarm/configurar_poc.py        # token da API pública (600)
ssh root@<manager> python3 - zabbix < deploy/swarm/configurar_poc.py       # integração; imprime a URL
ssh root@<manager> python3 - escala < deploy/swarm/configurar_poc.py       # rodízio diário + chain + rota
ssh root@<manager> python3 - regras < deploy/swarm/configurar_poc.py       # regra pessoal "notificar pelo Telegram"
```

Duas armadilhas: o sync de usuários do plugin usa cache, e só `?wait=true&force=true` leva
quem acabou de ser criado (com um lock de alguns minutos entre syncs); e usuário sincronizado
chega **sem** regra pessoal de notificação, de modo que a chain notificaria ninguém.

### 5. Telegram

1. Bot com **Group Privacy desligado** e **sem outro consumidor de updates**. O long polling
   chama `deleteWebhook` ao subir: bot com webhook de outro sistema perde esse sistema.
   Conferir antes com `getWebhookInfo` (url vazia), sem chamar `getUpdates`.
2. Pôr o token em `TELEGRAM_TOKEN=` no `$S/oncall.env`, `TELEGRAM_POLLING_REPLICAS=1` no
   `$S/stack.env`, e redeployar (último comando do passo 2).
3. Canal privado **com grupo de discussão ligado**, bot **admin nos dois**. O código de
   `telegram-codigo` é postado no canal; o OnCall o lê no encaminhamento automático para o
   grupo. Bot fora do grupo = o código chega como `channel_post` e é ignorado em silêncio.
4. Cada plantonista manda ao bot, no privado, o código de `telegram-usuario <login>`. Sem
   isso o botão de ack responde "You don't have a permission". O código tem 73 caracteres, acima
   do limite de 64 do deep link `?start=`, então vai como mensagem.
5. `telegram-rota <id do canal>` liga a rota padrão da integração ao canal.

## Rollback

No manager:

```bash
docker stack rm oncall-teste
docker service ls --filter label=com.docker.stack.namespace=oncall-teste   # vazio
docker network ls --filter name=oncall-teste                               # vazio
```

Isso para tudo e mantém os dados. Para apagar também os dados (irreversível):

```bash
rm -rf /mnt2/oncall-teste
```

No nó da stack (opcional, libera disco):

```bash
docker image rm \
  ghcr.io/appwrite/grafana-oncall@sha256:264563b3f2ee01000048612be729c0b93ad9ff7e4ca7dbe6cfe541e52659ccf4 \
  postgres@sha256:74935e72241653ca55e0414067e6d8763aceb8a810eb51b452253ec3dcfc4336 \
  redis@sha256:66b7f4c2f3c6ef1b298bbd7b13c806bc6f71e61f1136a4f58710daf2cb053b21 \
  grafana/grafana@sha256:3fd54ae1214669f8355f065ec9f6445d5279a3d77095ab048ca045685272429b
```

No Zabbix: `python3 deploy/zabbix/zbx_oncall_teste.py rollback` (dry-run) e depois com
`--apply`. No Telegram: revogar o token do bot (`/revoke` no BotFather) se ele apareceu em log.
