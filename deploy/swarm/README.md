# OnCall de teste num Docker Swarm

Prova de conceito do Grafana OnCall na **release OpenX `v1.24.10-openx.1`** deste repo
(15/10/2026): a v1.24.10 do fork `appwrite/grafana-oncall` mais as correções do #2 e do #3. A
PoC de 13 a 15/10/2026 rodou na v1.24.10 da Appwrite, com contornos por configuração; a seção
"Migrar a stack da PoC" leva uma stack daquela versão para esta.

**Não é produção**: o Grafana desta stack é **próprio**
(o Grafana de produção não recebe plugin de teste), e a integração com o Zabbix usa só objetos
de teste (`../zabbix/`).

## O que sobe

Stack `oncall-teste`, tudo fixado num nó (`ONCALL_NODE`), portas no endereço `ONCALL_HOST`.

| Serviço | Imagem | Porta | Dados |
|---|---|---|---|
| `engine` | `ghcr.io/openxbr/ox-oncall@sha256:e6d8…0d1d` (manifest da v1.24.10-openx.1) | **18080** (host) | — |
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
| `plugin-provisioning.yaml`, `initdb/10-grafana.sql` | aponta o plugin para o engine; cria o banco do Grafana |
| `configurar_poc.py` | roda no manager e configura tudo pela API: plugin, usuários, token, integração, escala, chain, Telegram |

## Por que assim (e o que quebrou no caminho)

- **Digest, não tag.** O `COMPATIBILITY.md` do fork diz que o digest do `release-manifest.json`
  é o artefato autoritativo. O plugin vem do zip da mesma release, com o SHA-256 conferido
  (`6d3e765f…eb54`) antes de extrair. Grafana 13.2.0, PostgreSQL 18.6 e Redis 8.2.9 são as
  versões da matriz testada do fork.
- **Um nó só, portas em `mode: host`.** Sem VXLAN entre os serviços e sem ingress mesh.
- **`endpoint_mode: dnsrr` em todos os serviços.** No Swarm da PoC (nós em LXC) o VIP da
  overlay não respondia nem entre containers do mesmo nó: `db` (VIP) dava timeout e `tasks.db`
  (IP da task) conectava; IPVS vazio no netns do container. Sintoma: migration e Grafana com
  "timed out waiting for a database connection" e o engine morto pelo healthcheck.
- **`GRAFANA_PUBLIC_URL`.** O install v2 do fork recusa (401) a URL que o plugin informa se
  ela não for igual a `GRAFANA_API_URL` ou a `GRAFANA_PUBLIC_URL`. O plugin informa o
  `root_url` do Grafana, então `GRAFANA_PUBLIC_URL` = `GF_SERVER_ROOT_URL`.
- **Release OpenX, não a da Appwrite.** A v1.24.10 tem três defeitos no Telegram que a PoC
  contornou por configuração: long polling morrendo com `SynchronousOnlyOperation` (contorno:
  `DJANGO_ALLOW_ASYNC_UNSAFE` no `telegram-polling`), token do bot no log do `httpx` (contorno:
  um `settings_openx.py` montado por bind) e `AttributeError` em todo `edited_message`. As
  correções estão no código desde o #2, e a `v1.24.10-openx.1` é a primeira release com elas,
  por isso a stack não tem mais contorno nenhum. O #3 fez o `publish.yml` publicar em
  `ghcr.io/openxbr/ox-oncall` (pacote público, o nó puxa sem login).
- **`license: OpenSource` no provisioning do plugin.** Sem `license` no `jsonData`, o backend do
  plugin deduz a licença pela própria versão. Até o #3 só `X.Y.Z` contava como OSS, e o plugin
  `1.24.10-openx.1` falharia em toda chamada. O valor explícito tira a stack dessa regra.
- **Telemetria desligada.** `SEND_ANONYMOUS_USAGE_STATS`, heartbeat e relay de notificação
  pelo Grafana Cloud em `False`. Sem o último em `False`, ligação e SMS iriam para a Grafana
  Cloud e não para o provedor configurado.

Exposição: porta em `mode: host` escuta em **todos** os IPs do nó. Para o teste basta; antes de
virar serviço, a stack precisa de TLS num proxy reverso e das portas fechadas.

## Deploy

`<manager>` = um manager do Swarm; `S=/mnt2/oncall-teste/stack`.

### 1. Na estação (clone deste repo)

```bash
scp deploy/swarm/oncall-teste.stack.yml deploy/swarm/gerar-env.sh deploy/swarm/plugin-provisioning.yaml deploy/swarm/stack.env.example deploy/swarm/initdb/10-grafana.sql root@<manager>:/root/
```

### 2. No manager

```bash
S=/mnt2/oncall-teste/stack
mkdir -p $S /mnt2/oncall-teste/postgres /mnt2/oncall-teste/redis /mnt2/oncall-teste/initdb \
  /mnt2/oncall-teste/grafana/data/plugins /mnt2/oncall-teste/grafana/provisioning/plugins
mv /root/oncall-teste.stack.yml /root/gerar-env.sh /root/plugin-provisioning.yaml $S/
cp /root/stack.env.example $S/stack.env      # editar ONCALL_HOST e ONCALL_NODE
mv /root/10-grafana.sql /mnt2/oncall-teste/initdb/
cp $S/plugin-provisioning.yaml /mnt2/oncall-teste/grafana/provisioning/plugins/grafana-oncall-app.yaml
sh $S/gerar-env.sh            # gera db.env, oncall.env, grafana.env (600), não sobrescreve

# Plugin: baixar, conferir o SHA-256 da release, extrair
cd /tmp && curl -fsSLO https://github.com/openxbr/ox-oncall/releases/download/v1.24.10-openx.1/grafana-oncall-app-1.24.10-openx.1.zip
echo "6d3e765f9e557eff0c074f52f072d63ed387dcb9c3e97fb15e030261de8aeb54  grafana-oncall-app-1.24.10-openx.1.zip" | sha256sum -c -
python3 -m zipfile -e grafana-oncall-app-1.24.10-openx.1.zip /mnt2/oncall-teste/grafana/data/plugins/
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
6. `org-nome <nome>` troca o nome que o bot mostra em "This channel is now linked to
   organization …". O OSS cria a organização com `SELF_HOSTED_ORG_TITLE` (padrão
   "Self-Hosted Organization") e o sync não mexe mais no nome. Numa instalação nova, já pôr
   `SELF_HOSTED_ORG_TITLE=<nome>` no `oncall.env` antes do primeiro install.

**Canal ou só DM.** Com a rota ligada a um canal, cada alerta aparece em 3 lugares:

- **canal:** o card do alerta, editado no mesmo post;
- **grupo de discussão:** os comentários do post, com "Actions available…" (os botões) e o
  "Alert group log";
- **DM de quem foi chamado:** só um link "👀 You are invited…" para o post.

Com `telegram-rota off` a rota para de publicar no canal, e a DM passa a levar o alerta
inteiro, com os botões e o log (`apps/telegram/models/connectors/personal.py`, `notify()`): 1
mensagem por pessoa chamada. Medido em 16/10/2026: escalada até o 2º passo, DM entregue 1,6 s
após o passo, ack pelo botão da DM e nenhum post no canal. O OnCall reedita a DM a cada linha
nova do log (34 `edit_message` em 15 min para 1 alerta): pesa no rate limit do bot se muitos
alertas abrirem juntos.

**Trocar o token do bot** (revogado no BotFather, por exemplo) tem duas pegadinhas:

- **Mudar o `oncall.env` não basta.** Na primeira subida, o OnCall copia o `TELEGRAM_TOKEN` do
  env para a tabela `base_livesetting` (`populate_settings_if_needed`), e daí em diante o banco
  ganha do env (`LiveSetting.get_setting`). O sintoma é o `telegram-polling` em loop com
  `telegram.error.InvalidToken: Unauthorized`. O valor tem de ser trocado na LiveSetting:
  pela tela **Env Variables** do plugin ou por `PUT live_settings/<id>/` com `name` e `value`.
- **Trocar a LiveSetting apaga as ligações do Telegram.** O hook `_reset_telegram_integration`
  (`apps/api/views/live_setting.py`) remove todos os canais e todas as contas pessoais
  ligadas, mesmo quando o bot é o mesmo e só o token mudou. Depois da troca é preciso refazer
  os passos 3 a 5, ou atualizar o valor direto na tabela, que não passa pelo hook, se as
  ligações devem ficar.

Para saber qual token vale sem expor nenhum dos dois: no nó da stack (`ONCALL_NODE`), os dois
hashes têm de sair iguais. Se a primeira linha vier vazia, não há LiveSetting e vale o env.

```bash
DB=$(docker ps -q -f name=oncall-teste_db)
echo "select md5(value #>> '{}') from base_livesetting where name='TELEGRAM_TOKEN';" | docker exec -i $DB psql -U oncall -d oncall -At
grep '^TELEGRAM_TOKEN=' /mnt2/oncall-teste/stack/oncall.env | cut -d= -f2- | tr -d '\n' | md5sum
```

## Migrar a stack da PoC (v1.24.10) para a v1.24.10-openx.1

Para uma stack que já roda com a imagem da Appwrite, o `settings_openx.py` e o plugin 1.24.10.
Os dados (`/mnt2/oncall-teste`) ficam. Entre as duas versões não há migration: além do #2 e
do #3, só entraram duas atualizações do OpenTelemetry no backend Go do plugin.

Se o token do bot mudou desde a subida da stack antiga, conferir antes os hashes de "Trocar o
token do bot". Na migração de 15/10/2026 o `oncall.env` já tinha o token novo, mas a LiveSetting
ainda tinha o revogado. O `telegram-polling` caiu 4 vezes com `InvalidToken: Unauthorized` até o
token ser gravado pela UI, e as ligações do Telegram precisaram ser refeitas.

Na estação (clone deste repo):

```bash
scp deploy/swarm/oncall-teste.stack.yml deploy/swarm/plugin-provisioning.yaml root@<manager>:/root/
```

No manager:

```bash
S=/mnt2/oncall-teste/stack
mv /root/oncall-teste.stack.yml /root/plugin-provisioning.yaml $S/
cp $S/plugin-provisioning.yaml /mnt2/oncall-teste/grafana/provisioning/plugins/grafana-oncall-app.yaml

# Plugin novo no lugar do 1.24.10
cd /tmp && curl -fsSLO https://github.com/openxbr/ox-oncall/releases/download/v1.24.10-openx.1/grafana-oncall-app-1.24.10-openx.1.zip
echo "6d3e765f9e557eff0c074f52f072d63ed387dcb9c3e97fb15e030261de8aeb54  grafana-oncall-app-1.24.10-openx.1.zip" | sha256sum -c -
rm -rf /mnt2/oncall-teste/grafana/data/plugins/grafana-oncall-app
python3 -m zipfile -e grafana-oncall-app-1.24.10-openx.1.zip /mnt2/oncall-teste/grafana/data/plugins/
chmod +x /mnt2/oncall-teste/grafana/data/plugins/grafana-oncall-app/gpx_grafana_oncall_app_linux_*
chown -R 472:0 /mnt2/oncall-teste/grafana/data

cd $S && set -a && . ./stack.env && set +a && docker stack deploy -c oncall-teste.stack.yml oncall-teste
# o serviço do Grafana não muda de spec; reiniciar para carregar o plugin novo
docker service update --force oncall-teste_grafana
```

Conferir, no manager:

```bash
docker service inspect oncall-teste_engine --format '{{.Spec.TaskTemplate.ContainerSpec.Image}}'   # ghcr.io/openxbr/ox-oncall@sha256:e6d8976f…
grep '"version"' /mnt2/oncall-teste/grafana/data/plugins/grafana-oncall-app/plugin.json             # 1.24.10-openx.1
docker service inspect oncall-teste_telegram-polling --format '{{.Spec.TaskTemplate.ContainerSpec.Env}}' | grep -c ASYNC_UNSAFE   # 0
docker stack ps oncall-teste --format '{{.Name}} {{.CurrentState}}' --filter desired-state=running   # tudo Running
```

Com tudo `Running`, apagar o arquivo do contorno antigo. Antes disso ele ainda serve a um
eventual rollback do Swarm para o spec anterior, que o monta:

```bash
rm /mnt2/oncall-teste/stack/settings_openx.py
```

Depois, com o bot ligado: código de verificação e botão de ack funcionando; zero linhas com
`api.telegram.org/bot` em `docker service logs oncall-teste_telegram-polling` e
`oncall-teste_celery`; e nenhum `AttributeError` de `forward_origin` depois de um ack (o ack
edita a mensagem do canal, e a edição volta como `edited_message`).

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
  ghcr.io/openxbr/ox-oncall@sha256:e6d8976fb8d58f3c67e6c9f75e2e7ab5cf54ece7f7c43304d184864f3b970d1d \
  postgres@sha256:74935e72241653ca55e0414067e6d8763aceb8a810eb51b452253ec3dcfc4336 \
  redis@sha256:66b7f4c2f3c6ef1b298bbd7b13c806bc6f71e61f1136a4f58710daf2cb053b21 \
  grafana/grafana@sha256:3fd54ae1214669f8355f065ec9f6445d5279a3d77095ab048ca045685272429b
```

No Zabbix: `python3 deploy/zabbix/zbx_oncall_teste.py rollback` (dry-run) e depois com
`--apply`. No Telegram: revogar o token do bot (`/revoke` no BotFather) se ele apareceu em log,
e trocar o token como em "Trocar o token do bot".
