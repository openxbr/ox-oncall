# Desenho — provedor de ligação `asterisk` (Asterisk + GoIP) para o ox-oncall

- **Data:** 13/10/2026 16:56 BRT
- **Base lida:** este repo (`openxbr/ox-oncall`), `main` em `bc442ab2` (merge de 25/09/2026 10:46 BRT,
  `v1.24.10-7-gbc442ab2`) [medido: `git log -1 --format='%ci' bc442ab2` → `14:46 +0100`, convertido]
- **Escopo:** só leitura de código + documentação pública do Asterisk. Nada foi implementado, alterado,
  executado contra servidor da OpenX, nem testado em laboratório.
- **Carimbos:** `[medido: …]` = li/rodei nesta tarefa; `[derivado: …]` = aritmética sobre algo medido;
  `[hipótese]` = não medi. Caminhos relativos a `engine/` salvo indicação.

---

## 0. Resumo

1. **Quem dá o ack:** `AlertGroup.acknowledge_by_user_or_backsync(user, action_source=ActionSource.PHONE)`
   (`apps/alerts/models/alert_group.py:702-768`), chamada **em nome do `PhoneCallRecord.receiver`** —
   o usuário que recebeu a ligação. Não há checagem de RBAC nesse caminho (`apps/twilioapp/gather.py:76-83`).
2. **Gancho pronto “dígito = ack” fora do Twilio:** existe **só no Zvonok**, e é específico dele: o
   postback traz `user_choice` e, se for igual à LiveSetting `ZVONOK_POSTBACK_USER_CHOICE_ACK`, ele dá o
   ack (`apps/zvonok/status_callback.py:70-79`). O Exotel **não** dá ack (só registra o status). No
   `apps/phone_notifications` **não existe** gancho genérico de dígito: o único contrato comum é
   `ProviderPhoneCall.phone_call_record` (`apps/phone_notifications/models/phone_call.py:60-82`). Cada
   provedor monta o próprio endpoint e chama a função de domínio. Um provedor `asterisk` copia esse padrão.
3. **Três armadilhas do código que o provedor novo tem de tratar** (nenhum provedor atual trata):
   - a função de ack **não é idempotente** e **tira o alerta de resolvido** (`alert_group.py:730-738`).
     Um “1” atrasado depois de alguém resolver pelo Telegram reabre o alerta;
   - exceção que escapa do provedor faz o Celery **repetir a tarefa sem limite**
     (`max_retries=None` fora de DEBUG, `apps/alerts/tasks/notify_user.py:369-374`), ou seja, liga de novo;
   - o registro da chamada só é salvo **depois** que `make_notification_call` retorna
     (`apps/phone_notifications/phone_backend.py:76-80`). Um callback muito rápido não acha a chamada.
4. **Recomendação** (detalhe na §4):
   - o OnCall dispara a chamada pela **API REST do ARI para o dialplan**, com `POST /ari/channels` e
     `context/extension`. Sem Stasis, sem websocket, sem AMI;
   - um canal `Local` faz o failover entre os 2 GoIP no dialplan;
   - o resultado volta por `CURL()` do dialplan para um endpoint novo `/asterisk/call_events/`,
     autenticado por um **token por chamada**;
   - **qualquer tecla = ack**;
   - **ficar na linha nunca dá ack** (fica como decisão do Renato, com os riscos na §4.7).

---

## 1. O contrato de provedor no engine

### 1.1 `PhoneProvider`, `ProviderFlags`, `get_phone_provider` — `apps/phone_notifications/phone_provider.py`

- `ProviderFlags` (`:13-31`) é um dataclass com 5 booleanos [medido: leitura `:27-31`]:
  `configured`, `test_sms`, `test_call`, `verification_call`, `verification_sms`. Ele só controla os
  botões da UI. É servido em `apps/api/serializers/organization.py:86-93` (`env_status.phone_provider`).
- `PhoneProvider(ABC)` (`:34-172`). O docstring (`:39-46`) dá a receita: implementar a classe, pôr as
  variáveis em settings e LiveSettings, registrar em `settings.PHONE_PROVIDERS`. Os métodos:
  - `make_notification_call(number, text) -> Optional[ProviderPhoneCall]` (`:49-69`). Devolve a
    instância **não salva**; quem salva e liga ao `PhoneCallRecord` é o `PhoneBackend`. Deve levantar
    `FailedToMakeCall` ou `ProviderNotSupports`.
  - `make_call(number, text)` (`:95-107`): usado pela chamada de teste e pelo relay OSS. Sem rastreio.
  - `send_notification_sms`, `send_sms`, `send_verification_sms`, `make_verification_call` e
    `finish_verification` (`:71-164`) levantam `ProviderNotSupports` por padrão.
  - `flags` (`:166-172`) é a única coisa abstrata.
- **Ponto crítico de desenho:** o provedor recebe **só `number` e `text`**. Ele **não recebe o alert
  group nem o usuário**. A ligação com o alerta é feita depois pelo `PhoneBackend`, via `link_and_save`.
  Por isso o provedor precisa gerar o próprio `call_id`, mandá-lo ao Asterisk e receber de volta.
  Twilio usa o `sid`, Zvonok o `call_id` e Exotel o `CallSid`.
- `get_phone_provider()` (`:175-188`):
  - instancia **todos** os provedores de `settings.PHONE_PROVIDERS` na primeira chamada e guarda num
    `dict` global do processo (`:181-183`). A instância é um **singleton por processo**: não guardar
    estado de chamada nela;
  - escolhe por `live_settings.PHONE_PROVIDER`. Se o nome não existe em `PHONE_PROVIDERS`, cai
    silenciosamente em `DEFAULT_PHONE_PROVIDER = "twilio"` (`:185-186`, `settings/base.py:999`).

### 1.2 `PhoneBackend.notify_by_call` — `apps/phone_notifications/phone_backend.py`

Quem chama: `perform_notification` (`apps/alerts/tasks/notify_user.py:375-444`), fila `critical`
[medido: `settings/celery_task_routes.py:106`]. Antes de chamar, ela pula se o alerta já está resolvido
(`notify_user.py:424-436`). O ack é checado antes, no `notify_user_task` (`notify_user.py:219-230`).

O fluxo:

1. `AlertGroupPhoneCallRenderer(alert_group).render()` monta o texto (`:61-62`). O template é fixo e
   **em inglês** (`apps/alerts/incident_appearance/renderers/phone_call_renderer.py:14-17`):
   `"You are invited to check an Alert Group from Grafana OnCall. Alert via {integration_name} with
   title {title} triggered {alert_count} times"`. Só o `{title}` é configurável por integração, via
   `phone_call_title_template` (`apps/alerts/models/alert_receive_channel.py:302`). O título passa por
   `clean_markup` e perde as aspas (`apps/alerts/incident_appearance/templaters/phone_call_templater.py:16-17`).
2. Cria um `PhoneCallRecord(represents_alert_group, receiver, notification_policy, exceeded_limit=False)`,
   ainda **não salvo** (`:64-69`).
3. Se `GRAFANA_CLOUD_NOTIFICATIONS_ENABLED` e é OSS, a chamada vai para a **Grafana Cloud** e não para o
   provedor (`:72-74`). O padrão é `True` [medido: `settings/base.py:70`]. **Tem de ir para `False`**,
   senão o provedor `asterisk` nunca é usado.
4. `_notify_by_provider_call` (`:105-117`):
   - exige `user.verified_phone_number` (`:109-110`);
   - checa o limite: `<= 0` levanta `CallsLimitExceeded`; `< 3` [medido: `:115`] acrescenta ao texto,
     **em inglês**, `"{n} phone calls left. Contact your admin."` (`:150-151`);
   - chama `phone_provider.make_notification_call(user.verified_phone_number, message)` (`:117`).
5. `record.save()` e, se o provedor devolveu algo, `provider_call.link_and_save(record)` (`:76-80`).
   **Isso acontece depois do originate** (ver a corrida na §4.4.4).
6. Exceções tratadas: 4 [medido: leitura `:81-90`] — `FailedToMakeCall`, `ProviderNotSupports`,
   `CallsLimitExceeded`, `NumberNotVerified`. Viram um `UserNotificationPolicyLogRecord` FAILED mais um
   sinal (`:92-103`). **Qualquer outra exceção sobe** para `perform_notification`, que tem
   `autoretry_for=(Exception,)`, `retry_backoff=True` e `max_retries=None` fora de DEBUG
   [medido: `notify_user.py:369-374`]. Ou seja, **re-liga sem limite**. O provedor tem de converter
   **tudo** em `FailedToMakeCall`.

**Limites:** `organization.phone_calls_left(user)` (`apps/user_management/models/organization.py:294-295`)
cai na `FreePublicBetaSubscriptionStrategy`, que é o padrão `pricing_version=0`
(`organization.py:236-238,268-270`). Ela soma **ligações + SMS** do usuário no dia contra
`PHONE_NOTIFICATIONS_LIMIT`, que vale 200 por padrão
[medido: `settings/base.py:87`; `apps/user_management/subscription_strategy/free_public_beta_subscription_strategy.py:32-50`].
O dia zera em `timezone.now().replace(hour=0)` com `TIME_ZONE="UTC"`, ou seja, **às 21:00 BRT**
[derivado: `free_public_beta_subscription_strategy.py:40` + `settings/base.py:482`]. Não há rate limit
por segundo nem fila no backend.

**Retries / reenvio:**
- o backend **não espera a ligação terminar**. O próximo passo da política pessoal é agendado com
  `countdown + NEXT_ESCALATION_DELAY` (`notify_user.py:352-363`). `NEXT_ESCALATION_DELAY` vale 5
  [medido: `apps/alerts/constants.py:19`], em segundos [derivado: é `countdown` de Celery, `notify_user.py:361`];
- uma política `ligar → ligar` sem `Wait` dispara a 2ª ligação ~5 s depois da 1ª, com a 1ª ainda tocando
  [derivado: idem];
- o “retry” de ligação não atendida é **a própria política de escalonamento**, não o provedor. Retry
  dentro do Asterisk (call file com `MaxRetries` etc.) duplicaria a política.

**Chamada de teste:** `make_test_call` usa `make_call` com o texto fixo `"It is a test call from Grafana
OnCall"` (`:435-443`). Não cria registro nem rastreio.

### 1.3 Modelos — `apps/phone_notifications/models/`

- `PhoneCallRecord` (`phone_call.py:27-57`), tabela `twilioapp_phonecall` (`:29`, legado):
  `represents_alert_group`, `receiver`, `notification_policy`, `exceeded_limit`, `created_at`,
  `grafana_cloud_notification`, mais `status`/`sid` legados.
- `ProviderPhoneCall` (`phone_call.py:60-82`), **abstrato**:
  - `phone_call_record = OneToOneField(..., null=False)`, com `related_name="%(app_label)s_%(class)s_related"`
    (`:72-78`);
  - `link_and_save(record)` (`:80-82`). Como o campo é `null=False`, **não dá para salvar o
    `ProviderPhoneCall` antes do `PhoneCallRecord`**.
- `SMSRecord`/`ProviderSMS` (`sms.py`) e `BannedPhoneNumber` (`banned_phone_number.py:22-61`). A
  verificação consulta a lista de banidos (`phone_backend.py:397,409`).
- Migrations do app: `0001_initial`, `0002_bannedphonenumber`, `0003_smsrecord_represents_bundle_uuid`
  [medido: `ls apps/phone_notifications/migrations`].

---

## 2. Como os provedores existentes fazem a volta do ack

### 2.1 Twilio (`apps/twilioapp`) — gather “press 1”

- **Montagem:** `make_notification_call` (`phone_provider.py:35-67`) monta TwiML
  `<Gather action=get_gather_url() method=POST num_digits=1>` com o texto e as instruções
  (`phone_provider.py:170-177`). Instruções: `"Press 1 to acknowledge, 2 to resolve, 3 to silence for
  30 minutes"` (`gather.py:94-95`). Pede status callback em 4 eventos [medido: `phone_provider.py:186`].
  Devolve `TwilioPhoneCall(status, sid)` (`:62-66`).
- **Endpoint que recebe o dígito:** `POST /twilioapp/gather/`, montado em `engine/urls.py:44` e
  `apps/twilioapp/urls.py:9`, tratado por `GatherView` (`views.py:54-61`). Lê `CallSid` e `Digits` do
  form.
- **Autenticação:** `AllowOnlyTwilio` (`views.py:19-46`). Procura `TwilioAccount` pelo `AccountSid` e
  valida a assinatura `X-Twilio-Signature` com `RequestValidator(auth_token)` (`:35-44`). **Sem
  auth_token**, só compara o `AccountSid` (`:45-46`), o que é fraco.
- **Domínio:**
  - `process_gather_data` (`gather.py:13-46`) aceita `1/2/3`; outro dígito repete o gather;
  - `process_digit` (`gather.py:49-87`) busca `TwilioPhoneCall.objects.filter(sid=…)`, depois
    `.phone_call_record`, e daí tira `alert_group` e `user = receiver` (`:63-77`);
  - `"1"` → `acknowledge_by_user_or_backsync(user, action_source=ActionSource.PHONE)` (`:83`);
  - `"2"` → `resolve_by_user_or_backsync` (`:85`);
  - `"3"` → `silence_by_user_or_backsync(silence_delay=1800)` (`:87`), ou seja 30 min
    [derivado: 1800 s / 60].
  - **Não há guarda de estado:** não checa se já está ack/resolvido.
- **Status:** `CallStatusCallback` (`views.py:77-85`) chama `update_twilio_call_status`
  (`status_callback.py:13-79`):
  - `completed` → log SUCCESS;
  - `busy`, `failed` e `no-answer` → log FAILED com `ERROR_NOTIFICATION_PHONE_CALL_LINE_BUSY`,
    `…_FAILED` e `…_NO_ANSWER` (`status_callback.py:82-90`).
- Teste de referência: `apps/twilioapp/tests/test_phone_calls.py:117` (`test_acknowledge_by_phone`).

### 2.2 Zvonok (`apps/zvonok`) — postback com `user_choice`

- **Originate:** `make_notification_call` (`phone_provider.py:24-59`) faz `POST` na API com
  `public_key`, `campaign_id`, `phone` e `text`. Se houver `ZVONOK_AUDIO_ID`, toca um áudio fixo no lugar
  do texto (`:28-31`). Devolve `ZvonokPhoneCall(status=IN_PROCESS, call_id, campaign_id)` (`:48-52`).
- **Endpoint:** `GET|POST /zvonok/call_status_events`, sem barra final (`engine/urls.py:80`,
  `apps/zvonok/urls.py:6`). Os parâmetros vêm **na query string** mesmo no POST (`views.py:48-52`). Os
  nomes são configuráveis: `ZVONOK_POSTBACK_CALL_ID`, `…_CAMPAIGN_ID`, `…_STATUS`, `…_USER_CHOICE` e
  `…_USER_CHOICE_ACK` (`settings/base.py:1016-1020`). A URL documentada é
  `…/zvonok/call_status_events?campaign_id={ct_campaign_id}&call_id={ct_call_id}&status={ct_status}&user_choice={ct_user_choice}`
  (`docs/sources/set-up/open-source/index.md:313-315`, caminho na raiz do repo).
- **Autenticação:** `AllowOnlyZvonok` (`views.py:11-33`) exige `call_id` e `campaign_id` presentes, o
  `campaign_id` igual ao configurado, um `ZvonokPhoneCall` existente com esse par, e `status` presente.
  **Não há segredo:** quem souber o `call_id` e o `campaign_id` dá ack.
- **Mapeamento para ack:** `update_zvonok_call_status` (`status_callback.py:12-79`):
  - grava o status e cria **um log record por postback**: `user`/`compl_finished` = SUCCESS, **qualquer
    outro**, inclusive `in_process`, = FAILED (`:45-51`);
  - emite o sinal (`:68`);
  - se `user_choice == live_settings.ZVONOK_POSTBACK_USER_CHOICE_ACK`, chama
    `acknowledge_by_user_or_backsync(receiver, ActionSource.PHONE)` (`:70-79`). Esse é o gancho
    “dígito = ack”. Está **desligado por padrão**, porque as duas settings vêm `None`
    (`settings/base.py:1019-1020`).
- **Defeitos a não copiar:**
  - `DETERMINANT` não tem `compl_nofinished`, que existe no `CHOICES` (`models/phone_call.py:22-51`).
    Postback com esse status é descartado (`status_callback.py:15-18`);
  - `call_id` sem índice (`models/phone_call.py:63-66`);
  - não há guarda de estado antes do ack.

### 2.3 Exotel (`apps/exotel`) — sem ack

- **Originate:** `_call_create` (`phone_provider.py:84-103`) chama `Calls/connect.json` com
  `From=number` e `Url=…/exoml/start/{EXOTEL_APP_ID}`. O fluxo de voz é um app configurado no painel da
  Exotel, então **o `message` do OnCall é ignorado** (`:28-31`). Pede `StatusCallback` em JSON (`:91-97`).
- **Endpoint:** `POST /exotel/call_status_events/` (`engine/urls.py:81`, `apps/exotel/urls.py:5-9`,
  `app_name="exotel"`).
- **Autenticação:** `AllowOnlyExotel` (`views.py:9-32`) exige um `CallSid` existente e
  `User-Agent == "Exotel Servers"`. O próprio comentário admite que não há validação confiável (`:27-29`).
- **Mapeamento:** `update_exotel_call_status` (`status_callback.py:17-73`): `completed` = SUCCESS, o
  resto = FAILED. O parâmetro `user_choice` existe na assinatura mas **nunca é usado**, e a view nem o
  passa (`views.py:43-46`). **Não há ack.**

### 2.4 MSG91 (`apps/msg91`) — referência de “provedor novo no fork”

Commit `a4e8fec4` (feat MSG91): 7 arquivos e 204 linhas inseridas [medido: `git show --stat a4e8fec4`].
Ele toca `apps/base/models/live_setting.py`, `settings/base.py`, o app e o teste, e a doc. É só SMS e
**sem model**, por isso não entrou em `INSTALLED_APPS` nem tem migration
(`settings/base.py:960` × `:1008`). É o molde de PR mais próximo do que o fork aceita.

### 2.5 Comparativo

| | Twilio | Zvonok | Exotel | proposta `asterisk` |
|---|---|---|---|---|
| dígito → ack | sim, `1` (`gather.py:83`) | sim, se configurado (`status_callback.py:70-79`) | não | sim, qualquer tecla (decisão) |
| também resolve/silencia | `2`/`3` | não | não | não (fazer pelo Telegram/web) |
| autenticação do callback | HMAC da Twilio (fraca sem token) | nenhuma (IDs) | User-Agent | token por chamada + HTTPS (+ IP) |
| guarda de estado antes do ack | não | não | — | sim (§4.4.3) |
| log por chamada | por evento | por postback | por callback | um por chamada (evento final) |
| texto do OnCall usado | sim (`Say`) | sim (TTS deles) | não | TTS ou áudio fixo (§4.6) |

### 2.6 A função de domínio — o que faz e o que não protege

`acknowledge_by_user_or_backsync(user, source_channel=None, action_source=None)`
(`apps/alerts/models/alert_group.py:702-768`), na ordem:
1. Se silenciado: `un_silence()` e log `TYPE_UN_SILENCE` (`:720-729`).
2. **Se resolvido: `unresolve()` e log `TYPE_UN_RESOLVED`** (`:730-738`).
3. `acknowledge(acknowledged_by_user=user, …)` (`:740`). O campo em si é idempotente
   (`if not self.acknowledged`, `:1858-1871`).
4. `stop_escalation()` e o lembrete de ack (`:744-746`).
5. **Sempre** cria um log `TYPE_ACK` e agenda `send_alert_group_signal`, que atualiza Slack e Telegram
   (`:748-761`). Uma segunda chamada gera log e mensagem duplicados.
6. Propaga para os alertas dependentes (`:763-766`).

A API pública guarda o estado antes de chamar: não faz ack em resolvido, anexado ou manutenção
(`apps/public_api/views/alert_groups.py:181-190`). Twilio e Zvonok **não guardam**. O provedor
`asterisk` deve imitar a API pública.

---

## 3. Onde registrar um provedor novo

| O quê | Onde | Como fazem zvonok/exotel |
|---|---|---|
| App em `INSTALLED_APPS` | `settings/base.py:959-960` | `if IS_OPEN_SOURCE: INSTALLED_APPS += ["apps.oss_installation", "apps.zvonok", "apps.exotel"]`. `IS_OPEN_SOURCE` = `ONCALL_LICENSE == "OpenSource"`, que é o padrão (`settings/base.py:16-20`) |
| Mapa de provedores | `settings/base.py:997-1008` | `PHONE_PROVIDERS["zvonok"] = "apps.zvonok.phone_provider.ZvonokPhoneProvider"`, dentro do mesmo `if IS_OPEN_SOURCE` |
| Seleção | `settings/base.py:1010` (env `PHONE_PROVIDER`) ou LiveSetting `PHONE_PROVIDER` | — |
| Valores padrão das variáveis | `settings/base.py:1012-1030` (`os.getenv(...)`) | **obrigatório**: a LiveSetting cai em `getattr(settings, name)` (`apps/base/models/live_setting.py:320-322`) e dá `AttributeError` se faltar |
| LiveSettings | `apps/base/models/live_setting.py`: `AVAILABLE_NAMES` (`:36-94`), `DESCRIPTIONS` (`:96-239`), `SECRET_SETTING_NAMES` (`:241-262`) | 10 nomes `ZVONOK_*` e 8 `EXOTEL_*` [medido: leitura `:65-82`] |
| Validação | `apps/base/utils.py:38-61`: `LiveSettingValidator.get_error()` chama `_check_<nome_minúsculo>` se existir; valor vazio vira `"Empty"`, salvo se o nome estiver em `EMPTY_VALID_NAMES` (`:39-43,53-54`) | zvonok não valida; `configured` do Exotel = `not LiveSetting.objects.filter(name__startswith="EXOTEL", error__isnull=False).exists()` (`apps/exotel/phone_provider.py:170`) |
| Rotas HTTP | `engine/urls.py:77-82`, dentro de `if settings.IS_OPEN_SOURCE:` | `path("zvonok/", include("apps.zvonok.urls"))`, `path("exotel/", include("apps.exotel.urls"))` |
| Fila de tarefa Celery (se criar task) | `settings/celery_task_routes.py` | o teste `common/tests/test_task_queue_assignment.py:28-34` **falha** se uma task nova não tiver rota |
| Model + migration | `apps/<app>/models/`, `apps/<app>/migrations/0001_initial.py` | `apps/zvonok/migrations/0001_initial.py` depende de `("phone_notifications", "0001_initial")` (`:11-13`); o `0002` só ajustou o `related_name` (`0002_…py:15-19`) |

Detalhes que mordem:
- O nome da LiveSetting tem `max_length=50` [medido: `live_setting.py:32`]. `save()` recusa nome fora de
  `AVAILABLE_NAMES` (`:324-330`).
- Tudo isso só vale com `FEATURE_LIVE_SETTINGS_ENABLED`, que é `True` por padrão
  [medido: `settings/base.py:60`]. Sem ela, só valem as variáveis de ambiente (`live_setting.py:281-282`).
- Opcionais (ex.: a URL do 2º Asterisk) têm de ir para `EMPTY_VALID_NAMES`. Senão ficam com erro
  `"Empty"` e o `flags.configured` no padrão `name__startswith` vira `False`.
- O DRF está com `DEFAULT_AUTHENTICATION_CLASSES: []` (`settings/base.py:326`): não há CSRF nem sessão
  nos endpoints de callback. A autenticação é 100% da `permission_class` do provedor. Os parsers aceitam
  JSON, form e multipart (`:321-325`).
- Para `reverse("asterisk:call_events")`, o `urls.py` precisa de `app_name` (o Exotel tem, `urls.py:5`;
  o Zvonok não).

**Migration de um `AsteriskPhoneCall` análogo ao `ZvonokPhoneCall`:** uma única `0001_initial.py` em
`apps/asterisk/migrations/`, dependendo de `("phone_notifications", "0003_smsrecord_represents_bundle_uuid")`,
a última hoje. Deve ser gerada por `makemigrations`, que roda o migration linter
(`settings/base.py:988-990`). O `related_name` herdado vira `asterisk_asteriskphonecall_related`
[derivado: `%(app_label)s_%(class)s_related`, `phone_call.py:75`]. O `PhoneCallRecord` não muda.

**Verificação de número:** o único caminho que grava `verified_phone_number` é
`PhoneBackend.verify_phone_number` → `provider.finish_verification` (`phone_backend.py:412-425`,
`apps/user_management/models/user.py:252-254`). Sem `verification_call`/`verification_sms` no provedor,
a UI não verifica. Restam duas opções:
- **(a)** um script de admin (`user.save_verified_phone_number("+55…")`) na fase 1;
- **(b)** implementar `make_verification_call` como o Zvonok (`phone_provider.py:116-148`): código de 6
  dígitos no cache por 10 min [medido: `randint(100000, 999999)`, `timeout=10*60`], falado pelo Asterisk
  com `SayDigits`.

---

## 4. Desenho proposto do provedor `asterisk`

### 4.1 Visão geral

```
perform_notification (Celery, fila critical)
  └─ PhoneBackend.notify_by_call
       └─ AsteriskPhoneProvider.make_notification_call(number, text)
            gera call_id (uuid) + token; POST https://<asterisk>/ari/channels  ──►  Asterisk
            devolve AsteriskPhoneCall não salvo                               │ Local/s@oncall-dial/n (;2)
       record.save(); link_and_save()                                         │   Dial(PJSIP/…@goip-vbd)
                                                                              │   falhou trunk? Dial(…@goip-sp4)
                                                                              │ atendeu → ;1 em oncall-ivr
                                                                              │   Playback + Read(qualquer tecla)
   POST /asterisk/call_events/  ◄── CURL(event=dtmf,digit=…)  ────────────────┤
     token ok → guarda de estado → acknowledge_by_user_or_backsync(receiver, PHONE)
     responde "ack" / "already" / "resolved" / "error" → a URA fala o resultado
   POST /asterisk/call_events/  ◄── CURL(event=finished,dialstatus=…,answeredtime=…) (;2, fim do Dial)
     um UserNotificationPolicyLogRecord SUCCESS/FAILED por chamada
```

### 4.2 Originate: AMI × ARI (Stasis) × ARI REST para o dialplan

| | **AMI** `Originate` | **ARI + Stasis** (`ChannelDtmfReceived`) | **ARI REST → dialplan** (recomendado) |
|---|---|---|---|
| Lado OnCall | cliente TCP de protocolo de linha (não há lib no `requirements.txt` [medido: grep `asterisk\|ami\|websocket`, 0 linhas]); login → Originate → logoff por chamada | **daemon permanente** com websocket (também não há lib), máquina de estado em Python e reconexão. O OnCall hoje só tem Django + Celery | `requests` (já é dependência [medido: `requirements.txt:409`]); um `POST` por chamada, cabe numa task Celery |
| Onde fica a lógica de URA | dialplan | Python | dialplan |
| Variáveis | `Variable: K=V`; texto livre exige cuidado com vírgula e escape. Mandar em base64 | no código | JSON no body (`variables`) [medido: doc ARI Channels], sem problema de escape |
| ID da chamada | `ChannelId` (versões recentes) [hipótese] | `channelId` | `channelId` e `otherChannelId` (o 2º é para canal `Local`); `409` se o ID já existe [medido: doc ARI Channels] |
| Saber “ocupado/não atendeu” | `OriginateResponse`/`Reason` exige **ficar ouvindo** a conexão. A extensão `failed` foi ignorada pelo AMI Originate em algumas versões (corrigido em 2017) e há relato de `Reason` sempre 0 com ocupado no 16.25.1 [derivado: issues ASTERISK-26115 e relato de 2022, via busca web] | eventos no websocket | pelo **dialplan**: `DIALSTATUS` do `Dial()` no canal `Local;2`, independente do originate |
| Segurança | AMI em texto puro (TLS opcional); expor só em rede privada | HTTP(S) + basic auth | HTTP(S) + basic auth (`http.conf` com TLS, `ari.conf`) |
| Falha do componente novo | — | daemon fora do ar = ligação perdida no meio | nada novo rodando no OnCall |
| Testável só com Asterisk | sim | não (precisa do daemon) | sim (`curl` no ARI) |

**Recomendação:** ARI REST para o dialplan. Em `POST /ari/channels`, `app` e `context`/`extension` são
mutuamente exclusivos [medido: doc ARI Channels]. Usar `context`/`extension` dá originate HTTP sem
Stasis. O `timeout` padrão do ARI é 30 s [medido: doc ARI Channels]. Com failover entre 2 trunks, ele
tem de ser maior: proposta 75 s [hipótese], que cobre 30 s de toque mais o failover mais margem
[derivado: soma]. Stasis só vale a pena se um dia a lógica precisar de Python (ex.: reconhecimento de
fala).

**Como o provedor faz o originate** (esboço, não testado):

```python
# apps/asterisk/phone_provider.py — ESBOÇO
def make_notification_call(self, number, message):
    call = AsteriskPhoneCall(call_id=uuid4().hex, token=secrets.token_urlsafe(24),
                             status=AsteriskCallStatuses.ORIGINATING)
    try:
        node = self._originate(number, message, call)    # tenta ASTERISK_ARI_URLS em ordem
    except Exception as e:                                # TUDO vira FailedToMakeCall (§1.2, retry infinito)
        raise FailedToMakeCall(graceful_msg=f"Asterisk originate failed for {number}") from e
    call.asterisk_node = node
    return call                                           # PhoneBackend salva o record e faz link_and_save

def _originate(self, number, message, call):
    params = {"endpoint": live_settings.ASTERISK_DIAL_ENDPOINT,       # "Local/s@oncall-dial/n"
              "context": live_settings.ASTERISK_IVR_CONTEXT,          # "oncall-ivr"
              "extension": "s", "priority": 1,
              "channelId": call.call_id, "otherChannelId": call.call_id + "-2",
              "timeout": live_settings.ASTERISK_ORIGINATE_TIMEOUT}
    body = {"variables": {"__ONCALL_CALL_ID": call.call_id, "__ONCALL_TOKEN": call.token,
                          "__ONCALL_NUMBER": number, "__ONCALL_TEXT": message}}
    for url in urls:
        try:
            r = requests.post(f"{url}/ari/channels", params=params, json=body,
                              auth=(user, pwd), timeout=(3, 5))
        except requests.ConnectionError:   # só falha de CONEXÃO passa para o próximo Asterisk;
            continue                       # timeout de leitura NÃO, senão o originate pode ter saído 2 vezes
        r.raise_for_status(); return url
    raise FailedToMakeCall(...)
```

### 4.3 Dialplan (esboço, **não testado**; nomes e sintaxe a validar na versão escolhida)

```
[globals]
ONCALL_CB_URL=https://<base-url-do-oncall>/asterisk/call_events/   ; fixo no Asterisk, não vem do OnCall

[oncall-dial]                                   ; roda no Local;2
exten => s,1,NoOp(oncall ${ONCALL_CALL_ID} -> ${ONCALL_NUMBER})
 same => n,Set(GROUP(oncall)=${ONCALL_NUMBER})
 same => n,GotoIf($[${GROUP_COUNT(${ONCALL_NUMBER}@oncall)} > 1]?coalesced)  ; 1 chamada por número
 same => n,Set(NUM=<normalização +55… → formato que o GoIP disca; ver §5>)
 same => n,Set(TRUNK=goip-vbd)
 same => n,Dial(PJSIP/${NUM}@goip-vbd,30)
 same => n,GotoIf($["${DIALSTATUS}" = "CHANUNAVAIL" | "${DIALSTATUS}" = "CONGESTION"]?alt:report)
 same => n(alt),Set(TRUNK=goip-sp4)
 same => n,Dial(PJSIP/${NUM}@goip-sp4,30)
 same => n(report),Set(CURLOPT(conntimeout)=3)
 same => n,Set(CURLOPT(httptimeout)=5)
 same => n,Set(R=${CURL(${ONCALL_CB_URL},call_id=${ONCALL_CALL_ID}&token=${ONCALL_TOKEN}&event=finished&dialstatus=${DIALSTATUS}&answeredtime=${ANSWEREDTIME}&trunk=${TRUNK})})
 same => n,Hangup()
 same => n(coalesced),Set(DIALSTATUS=COALESCED)
 same => n,Goto(report)

[oncall-ivr]                                    ; roda no Local;1, depois que o celular atende
exten => s,1,Answer()
 same => n,Playback(oncall/alerta-openx)                         ; v1: áudio fixo (§4.6)
 same => n,Read(D,oncall/pressione-qualquer-tecla,1,,3,10)
 same => n,GotoIf($["${D}" = ""]?nodigit)
 same => n,Set(R=${CURL(${ONCALL_CB_URL},call_id=${ONCALL_CALL_ID}&token=${ONCALL_TOKEN}&event=dtmf&digit=${URIENCODE(${D})})})
 same => n,Playback(oncall/resp-${IF($["${R}" = "ack" | "${R}" = "already" | "${R}" = "resolved"]?${R}:erro)})
 same => n,Hangup()
 same => n(nodigit),Playback(oncall/sem-confirmacao)             ; "não recebi a tecla; confirme pelo Telegram"
 same => n,Hangup()
```

Notas:
- **Por que `Local`:** o failover entre GoIPs e o relatório de `DIALSTATUS` ficam no dialplan.
  Ocupado ou não atendido não dependem do `OriginateResponse` nem da extensão `failed` (ver a tabela).
- **Failover só em `CHANUNAVAIL`/`CONGESTION`** (trunk/GSM). `BUSY`/`NOANSWER` são decisão humana: quem
  tenta de novo é a política de escalonamento.
- **`Read`:** na doc atual o arquivo de prompt é obrigatório [medido: doc Read]. Por isso a proposta
  `Read(D,<prompt>,1,,3,10)` e não `Read(DIGITO,,1,,3,10)`. Com 3 tentativas × 10 s, são até 30 s de
  espera mais os prompts [derivado: parâmetros propostos]. A doc não diz se o prompt aceita barge-in; o
  `READSTATUS` tem `INTERRUPTED` [medido: doc Read]. Confirmar em laboratório.
- **A resposta do callback vira fala:** a URA só diz “confirmado” se o OnCall respondeu `ack`. Se o
  `CURL` falhar, o plantonista ouve que tem de confirmar pelo Telegram. A falha de rede vira informação,
  não um falso “ok”. O `CURL()` devolve o corpo da resposta: o endpoint responde **texto puro**
  (`HttpResponse("ack")`). O `Response("ack")` do DRF viraria `"ack"` com aspas.
- **`GROUP_COUNT`:** numa tempestade de alertas o mesmo plantonista receberia N ligações, uma por alert
  group. O backend **não agrupa** ligações (só SMS tem bundle: `phone_backend.py:187-235`). O limite de 1
  por número evita fila no GoIP. A excedente volta como `COALESCED` e vira log FAILED.
- **Política pessoal:** recomendar `ligar → Wait ≥ 2 min → ligar` [derivado: 30 s de toque + ~30-45 s de
  URA + margem]. Sem `Wait`, a 2ª ligação sai ~5 s depois da 1ª (§1.2).

### 4.4 Lado OnCall

#### 4.4.1 Model `AsteriskPhoneCall` (`apps/asterisk/models/phone_call.py`)

```python
class AsteriskPhoneCall(ProviderPhoneCall, models.Model):
    created_at  = models.DateTimeField(auto_now_add=True)
    call_id     = models.CharField(max_length=64, unique=True)        # = channelId do ARI (índice único)
    token       = models.CharField(max_length=64)
    status      = models.PositiveSmallIntegerField(null=True, choices=AsteriskCallStatuses.CHOICES)
    dial_status = models.CharField(max_length=16, blank=True)         # DIALSTATUS cru
    trunk       = models.CharField(max_length=32, blank=True)         # goip-vbd / goip-sp4
    asterisk_node = models.CharField(max_length=128, blank=True)
    digit       = models.CharField(max_length=4, blank=True)
    answered_seconds = models.PositiveIntegerField(null=True)         # ANSWEREDTIME
    acked_at    = models.DateTimeField(null=True)                     # trava de idempotência do ack
    finished_at = models.DateTimeField(null=True)                     # trava do log final
```

#### 4.4.2 Endpoint `POST /asterisk/call_events/`

- `engine/urls.py`: dentro de `if settings.IS_OPEN_SOURCE:` (`:77-82`), `path("asterisk/",
  include("apps.asterisk.urls"))`. `apps/asterisk/urls.py` com `app_name="asterisk"` e
  `path("call_events/", CallEventView.as_view(), name="call_events")`.
- Campos de form: `call_id`, `token`, `event ∈ {dtmf, finished}`, `digit`, `dialstatus`,
  `answeredtime`, `trunk`.
- `AllowOnlyAsterisk` (padrão `BasePermission`, como `apps/zvonok/views.py:11`):
  - busca `AsteriskPhoneCall` por `call_id`;
  - `hmac.compare_digest(token, call.token)`;
  - opcional: o IP de origem em `ASTERISK_CALLBACK_ALLOWED_IPS`;
  - recusa se `created_at` passou de uma janela, ex. 15 min [hipótese].
- Mapeamento do `event=finished` para **um** `UserNotificationPolicyLogRecord` por chamada (trava em
  `finished_at`), com o mesmo formato de `apps/twilioapp/status_callback.py:58-79` e o sinal como em
  `apps/zvonok/status_callback.py:68`:

  | `DIALSTATUS` | tipo | `notification_error_code` |
  |---|---|---|
  | `ANSWER` | `TYPE_PERSONAL_NOTIFICATION_SUCCESS` (atendeu ≠ ack) | — |
  | `BUSY` | FAILED | `ERROR_NOTIFICATION_PHONE_CALL_LINE_BUSY` |
  | `NOANSWER` | FAILED | `ERROR_NOTIFICATION_PHONE_CALL_NO_ANSWER` |
  | `CHANUNAVAIL`, `CONGESTION`, `COALESCED`, outros | FAILED | `ERROR_NOTIFICATION_PHONE_CALL_FAILED` |

  Os códigos existem em `apps/base/models/user_notification_policy_log_record.py:91-93`.

#### 4.4.3 Ack com guarda e idempotência (`event=dtmf`)

```python
def process_dtmf(call, digit) -> str:            # devolve o texto que a URA vai falar
    if digit not in live_settings.ASTERISK_ACK_DIGITS:   # padrão: "0123456789*#" = qualquer tecla
        return "error"
    with transaction.atomic():
        if not AsteriskPhoneCall.objects.filter(pk=call.pk, acked_at__isnull=True) \
                .update(acked_at=timezone.now(), digit=digit):
            return "already"                     # CAS: segundo callback do mesmo call_id não faz nada
        rec = call.phone_call_record
        ag = AlertGroup.objects.select_for_update().get(pk=rec.represents_alert_group_id)
        if ag.resolved:          return "resolved"    # NÃO chamar: a função desresolveria (alert_group.py:730-738)
        if ag.acknowledged:      return "already"     # evita log TYPE_ACK + mensagem duplicados (:748-761)
        if ag.root_alert_group_id or ag.is_maintenance_incident:
            return "error"                            # mesmas guardas da API pública (alert_groups.py:181-188)
    ag.acknowledge_by_user_or_backsync(rec.receiver, action_source=ActionSource.PHONE)
    return "ack"
```

- **Duplicata do mesmo dígito:** a trava em `acked_at` é um compare-and-swap via `UPDATE … WHERE
  acked_at IS NULL`.
- **Ack concorrente pelo Telegram:** o `select_for_update` estreita a janela, mas não a fecha, porque a
  chamada de domínio fica fora do bloco. O pior caso é um log de ack a mais. É aceitável.
- **Callback perdido** (OnCall fora no instante): o `CURL` falha e o plantonista ouve “não consegui
  confirmar”. A escalada continua. O erro vai na **direção segura**: mais ligação, não menos.

#### 4.4.4 A corrida “callback antes do registro”

O `PhoneCallRecord` e o `AsteriskPhoneCall` só existem depois de `make_notification_call` retornar
(`phone_backend.py:76-80`). Um `event=dtmf` exige atender e ouvir o prompt, então chega segundos depois:
na prática não corre [hipótese]. Um `event=finished` com `CHANUNAVAIL` nos 2 trunks pode chegar em
milissegundos [hipótese].

Opções:
- **(a)**, recomendada pela simplicidade: aceitar a perda. Só se perde um log FAILED e a escalada
  continua igual. Responder `404` e logar `warning`, como Twilio e Zvonok fazem (`status_callback.py:38-40`
  e `:20-23`);
- **(b)** o dialplan tenta de novo uma vez após `Wait(2)` quando o `CURL` não devolve `ok`;
- **(c)** uma task Celery com `countdown` reprocessa o evento. Exige rota em
  `settings/celery_task_routes.py` (§3) e cuidado: dá para enfileirar tarefas sem token válido.

### 4.5 Autenticação do callback

1. **Token por chamada (recomendado):**
   - `secrets.token_urlsafe(24)`, ou seja 192 bits [derivado: 24 bytes × 8];
   - gravado no `AsteriskPhoneCall`, viaja ao Asterisk no body do `POST /ari/channels` (HTTPS) e volta no
     callback. Comparar com `hmac.compare_digest`;
   - funciona com `CURL()` puro: não precisa configurar segredo no Asterisk nem de AGI;
   - risco: o token aparece em log verboso/CDR do Asterisk. Quem lê esses logs pode confirmar uma
     chamada pendente, mas já tem acesso ao sistema de telefonia.
2. **HMAC com segredo compartilhado:** só vale se já houver AGI em Python (ex.: por causa do TTS). As
   funções de dialplan que conheço (`SHA1`, `MD5`) não fazem HMAC [hipótese]. Padrão a seguir:
   `apps/slack/views.py:87-93` (timestamp + `compare_digest`). **Não copiar**
   `apps/chatops_proxy/events/signature.py:33`, que compara com `!=`.
3. **Complementos:** HTTPS com `CURLOPT(ssl_verifypeer)=1` [medido: a opção existe na doc CURLOPT],
   allowlist de IP de origem, e a janela de validade do `call_id`.
4. **Não copiar:** Zvonok (sem segredo, `views.py:11-33`) e Exotel (User-Agent, `views.py:26-32`).
5. **Do outro lado:** ARI só em interface privada e com TLS, firewall liberando só os hosts do OnCall, e
   usuário ARI exclusivo. A senha vai para `SECRET_SETTING_NAMES`.

### 4.6 Texto e TTS em pt-BR

- **O texto chega pronto e em inglês** (§1.2). Há três saídas, e a escolha é do Renato:
  - **(a)** traduzir o `TEMPLATE` do fork (`phone_call_renderer.py:14-17`) e o aviso de limite
    (`phone_backend.py:150-151`). São poucas linhas, mas é core: vira conflito em todo merge do upstream;
  - **(b)** o provedor ignora `text` e usa prompts fixos em pt-BR;
  - **(c)** o provedor extrai `{title}` do texto inglês por regex. É frágil, não recomendo.
- **Motores:**
  - **Piper:** offline, com vozes neurais pt_BR (cadu, edresson, faber e jeff, 4 vozes)
    [medido: `rhasspy/piper/VOICES.md`]. Gera WAV que precisa ser reamostrado para 8 kHz mono para o
    canal G.711 [hipótese].
  - **espeak-ng:** tem pt-br, leve, voz robótica [hipótese].
  - **Flite:** sem pt-BR [hipótese], descartar.
  - **gTTS e TTS de nuvem:** dependem de internet e de serviço externo no caminho de acordar alguém.
    Descartar para este uso.
- **Onde sintetizar:**
  - no host do Asterisk, num **AGI em Python** que lê `ONCALL_TEXT` via `GET VARIABLE`. **Nunca**
    interpolar o texto em `System()`: o título vem do alerta, é texto livre, e isso abre injeção de shell;
  - sintetizar **antes do `Dial`**, no `oncall-dial`, para não haver silêncio depois que o celular atende;
  - se falhar ou demorar, cair no áudio fixo.
- **Proposta em fases:**
  - **v1 sem TTS.** Prompts fixos gerados uma vez com o Piper e versionados: “Alerta da OpenX. Pressione
    qualquer tecla para confirmar.” O detalhe do alerta o plantonista lê no Telegram. O caminho que
    acorda não depende do TTS.
  - **v2:** TTS do título com timeout e fallback.
  - Contagem falada (`SayNumber`) precisa de sons pt_BR no Asterisk; o pacote oficial pode não ter
    [hipótese].

### 4.7 DTMF não confiável via GoIP — o que dá para fazer

**Por que é frágil** (mecanismo provável, não medido) [hipótese]:
- o plantonista aperta a tecla no próprio celular; a rede dele regenera o tom **dentro do áudio** até o
  módulo GSM do GoIP;
- no caminho há codec GSM/AMR e às vezes VoLTE com interconexão entre operadoras;
- depois o GoIP precisa **detectar o tom no áudio comprimido** e convertê-lo, ou entregar o áudio para o
  Asterisk detectar.

Há relato de GoIP4 em que 1 SIM de 4 não reconhecia DTMF e os outros 3, de outras operadoras,
funcionavam na mesma configuração [medido: WebFetch do thread 3CX “DTMF issue” 107537]. Ou seja, **o
problema pode ser por operadora/chip, não por equipamento.**

**Modos** (combinar igual nos dois lados):

| GoIP | Asterisk (`pjsip.conf`, `dtmf_mode`) | quando |
|---|---|---|
| RFC2833 | `rfc4733`, que é o padrão [medido: doc res_pjsip] | primeira tentativa. O GoIP detecta o tom e manda evento RTP |
| Inband | `inband`, só com `alaw`/`ulaw` (G.711); com G.729 a detecção quebra [hipótese] | se o detector do GoIP for ruim. O Asterisk detecta (ajustes de `dsp.conf` a confirmar) [hipótese] |
| SIP INFO | `info` | se o firmware do GoIP fizer melhor por INFO |
| — | `auto` = RFC4733 se o outro lado anunciar, senão inband [medido: doc res_pjsip] | para atravessar mudança de configuração do GoIP |

Medidas que **não dependem** de escolher o modo certo:
1. **Qualquer tecla confirma.** Um “1” detectado como “4” ainda prova que um humano apertou algo.
   Resolver e silenciar ficam fora do telefone: Telegram, que já tem botão de ack
   (`apps/telegram/updates/update_handlers/button_press.py:117`), e web.
2. **Repetir o prompt** (3 tentativas, §4.3) e **dizer o resultado** (§4.3): o plantonista sabe quando
   precisa confirmar por outro meio.
3. **Ack fora da ligação:** a ligação serve para **acordar**. Se o DTMF falhar, o ack pelo Telegram para
   a escalada do mesmo jeito. O custo de um DTMF perdido é uma ligação a mais ou a escalada para o
   próximo da fila depois do `Wait`.
4. **Rota por operadora:** se o teste (§5) mostrar que o DTMF depende do par chip × operadora do
   plantonista, o dialplan escolhe primeiro o GoIP da mesma operadora. Uma tabela simples no AstDB
   resolve.

**Fallback “atendeu e ficou X s na linha = ack” — decisão do Renato.** Riscos:
- **Caixa postal atende.** Com o celular desligado ou fora de área, a operadora desvia para a caixa,
  que **atende** e fica na linha gravando. O `DIALSTATUS` dá `ANSWER` e o `ANSWEREDTIME` sobe. Seria
  **ack falso justamente quando o plantonista está inalcançável**: o pior caso possível, porque para a
  escalada [hipótese sobre o comportamento das operadoras].
- **Atende dormindo e larga o telefone:** ack sem ninguém acordado.
- **O GoIP sinaliza atendimento cedo demais:** alguns gateways GSM mandam `200 OK` na conexão GSM, antes
  do atendimento real [hipótese]. O tempo “na linha” contaria toque.
- Mitigações parciais:
  - desligar a caixa postal das linhas dos plantonistas;
  - usar `ANSWEREDTIME` de vários segundos com a chamada derrubada só pelo celular (`hangup` do lado
    dele);
  - detecção de voz (`TALK_DETECT`/AMD), que não separa saudação de caixa postal de pessoa [hipótese].
- **Recomendação:** tempo na linha **nunca** dá ack. Ele só marca SUCCESS (“atendida”) no log, como o
  Twilio já faz com `completed`. Se o Renato quiser o fallback mesmo assim, deixar atrás de LiveSetting
  desligada por padrão e só depois de medir a taxa de DTMF (§5, item 7).

### 4.8 Dois GoIP em POPs diferentes: um Asterisk ou dois

- **Failover de trunk** (vale nos dois cenários):
  - `qualify` nos endpoints PJSIP dos GoIP, para um GoIP fora do ar dar `CHANUNAVAIL` imediato e não
    timeout;
  - `Dial` sequencial VBD → SP4 (§4.3);
  - **chips de operadoras diferentes** em cada POP, para cobrir também queda de operadora.

| | **1 Asterisk** (ex.: VBD) + 2 trunks | **2 Asterisks** (VBD e SP4), cada um com os 2 GoIP |
|---|---|---|
| Ponto único de falha | o host do Asterisk e o POP dele | some (o OnCall tenta `ASTERISK_ARI_URLS` em ordem, §4.2) |
| Configuração | uma | duplicada; dialplan e prompts versionados num repo e aplicados nos dois |
| Caminho até o GoIP remoto | RTP e SIP VBD↔SP4 pelo backbone | local para o seu GoIP, remoto para o outro |
| Duplicar ligação | — | só se o failover do originate disparar com timeout de leitura; o esboço só troca de nó em erro de **conexão** (§4.2) |
| Código no OnCall | uma URL | lista de URLs, poucas linhas a mais |

Sugestão: começar com **1 Asterisk e 2 trunks** e subir o 2º quando o próprio OnCall estiver em mais
de um POP. Não adianta o Asterisk sobreviver se o OnCall cair junto. É decisão do Renato, que depende de
onde o OnCall vai rodar. Rodar o Asterisk em VM ou LXC com rede direta, **sem** NAT ou overlay do Docker
para RTP: o Swarm em LXC já deu trabalho com ingress e com o VIP da overlay (ver `../swarm/README.md`).

### 4.9 Monitorar o próprio caminho de ligação

A lição do Telegram vale aqui: o canal de alerta já ficou semanas mudo sem ninguém notar
[derivado: incidente de 21/08–09/10/2026 registrado internamente]. O caminho de acordar precisa de
monitoramento próprio no Zabbix:
- Asterisk vivo: HTTP agent em `GET /ari/asterisk/info`;
- estado dos endpoints PJSIP `goip-vbd`/`goip-sp4` via ARI `/endpoints`;
- registro GSM, sinal e saldo dos chips, pela página de status do GoIP. Se o firmware não expõe SNMP,
  raspar o HTTP [hipótese];
- **chamada sintética cruzada:** o GoIP-VBD liga para o chip do GoIP-SP4; o Asterisk atende a entrada
  e manda um tom “1”. Isso testa ponta a ponta o GSM, a interconexão entre operadoras e a detecção de
  DTMF, e mede a taxa de sucesso ao longo do tempo sem incomodar ninguém [hipótese de viabilidade].

---

## 5. O que depende do Renato

1. **GoIP:**
   - modelo e **número de canais** (o GoIP-1 tem 1 canal [hipótese]; isso define se dá para fazer 2
     ligações ao mesmo tempo);
   - versão de firmware;
   - instalação física nos 2 POPs, com **sinal GSM no rack** (antena externa?);
   - IP de gerência e VLAN.
2. **Chips e operadoras:**
   - qual operadora em cada POP (recomendo diferentes) e plano de voz com minutos para móvel de qualquer
     operadora;
   - PIN desligado;
   - **confirmar se o contrato permite uso em gateway GSM**: operadora costuma bloquear “SIM box”
     [hipótese]. O volume baixo ajuda, mas não garante;
   - ver o risco do 2G: em jul/2026 a Vivo disse que quer desligar o 2G “o quanto antes”, sem data, e as
     faixas começam a vencer em 2028 [derivado: tecnoblog/teletime via busca web, não confirmado na
     Anatel]. O GoIP é só 2G.
3. **Trunk SIP GoIP ↔ Asterisk:**
   - modo (o GoIP registra no Asterisk ou o Asterisk autentica por IP);
   - codec (forçar `alaw`);
   - DTMF (§4.7);
   - caminho de rede VBD↔SP4.
4. **Onde roda o Asterisk:** host (VM ou LXC), POP, 1 ou 2 instâncias (§4.8), versão LTS, quem mantém.
   Firewall: OnCall → ARI (TLS) e Asterisk → `BASE_URL` do OnCall (HTTPS).
5. **Número de origem e identificação:**
   - o bina é o número do chip; a rede GSM não deixa trocar [hipótese]. Com 2 GoIP são 2 números;
   - cada plantonista salva os dois como contato e libera o “furar o Não Perturbe” (Emergency Bypass no
     iOS, contato prioritário no Android). **Sem isso a ligação não acorda ninguém**;
   - decidir sobre a caixa postal das linhas dos plantonistas (§4.7).
6. **Formato do número:** como o GoIP disca (`+55DDD…`, `0+CSP+DDD+…` ou local). Testar e definir a
   normalização no dialplan.
7. **Testes reais de DTMF por operadora** (a medida que decide §4.7):
   - matriz chip do GoIP (operadora) × operadora do plantonista × tecnologia do aparelho (2G/4G VoLTE) ×
     modo (RFC2833/inband/INFO);
   - N tentativas por célula, anotando o dígito apertado e o dígito recebido (`rtp set debug`/log do
     `Read`);
   - medir também se o GoIP **só sinaliza atendimento quando o celular atende** (supervisão de
     atendimento) e quanto tempo o telefone toca antes da operadora desviar para a caixa.
8. **Decisões de produto:**
   - qualquer tecla ou só “1”;
   - fallback por tempo na linha (recomendo não);
   - template pt-BR no core ou prompt fixo (§4.6);
   - TTS na v1 ou não;
   - política pessoal com `Wait` entre ligações (§4.3);
   - **quais alertas ligam**: hoje são 451 alertas em 24 h [derivado: `PENDENCIAS.md` item 42], e ligar
     para todos não é opção.
9. **Verificação dos números:** script de admin na fase 1 ou ligação de verificação (§3).
10. **Manutenção do fork:** as mudanças em `settings/base.py`, `engine/urls.py` e `live_setting.py` (e
    no `phone_call_renderer.py`, se escolher pt-BR no core) entram em conflito a cada atualização do
    upstream `appwrite/grafana-oncall`. Decidir se vira PR upstream (como o MSG91) ou um patch da OpenX.

---

## 6. Esforço estimado [todas as linhas são hipótese] e riscos

Referência de tamanho:
- app Zvonok: 367 linhas sem testes e migrations [medido: `wc -l apps/zvonok/{*.py,models/*.py}`];
- app Exotel: 349 linhas [medido: idem];
- commit do MSG91: 204 linhas [medido: `git show --stat a4e8fec4`].

O `asterisk` fica entre o Zvonok e o Twilio, porque soma callback, guardas e idempotência.

| Parte | Esforço [hipótese] | Bloqueado por |
|---|---|---|
| App `apps/asterisk`: provedor, model, migration, view + permission, mapeamento de status, guardas de ack, LiveSettings, settings, urls, testes no molde de `twilioapp/tests/test_phone_calls.py:117` | 2–3 dias | nada (dá para fazer com ARI falso) |
| Dialplan + PJSIP + prompts fixos + ARI/TLS no Asterisk | 1–2 dias | host do Asterisk (§5.4) |
| GoIP: instalação, trunk, formato de número | 1–2 dias de trabalho, prazo de calendário da operadora | §5.1–5.3 |
| Bateria de testes de DTMF (§5.7) e ajuste de modo | 1–2 dias, mais repetição por operadora | GoIP no ar, voluntários com chips de cada operadora |
| TTS Piper via AGI (v2) | 1 dia | decisão §4.6 |
| Ligação de verificação (`SayDigits`) | 0,5–1 dia | sons pt_BR |
| Template pt-BR no core | 0,5 dia | decisão §5.8 |
| Monitoramento no Zabbix + ligação sintética cruzada | 1–2 dias | os 2 GoIP no ar |
| 2º Asterisk (opcional) | 0,5–1 dia | §4.8 |

| Risco | Efeito | Mitigação |
|---|---|---|
| DTMF não detectado (operadora/chip) | sem ack pelo telefone | qualquer tecla, URA fala o resultado, ack pelo Telegram, rota por operadora, **medir antes** (§5.7) |
| Caixa postal atende | “SUCCESS” falso no log; **ack falso** se adotar o fallback por tempo | tempo na linha nunca dá ack; desligar caixa postal |
| Ack desresolve um alerta resolvido | alerta reaberto por um “1” atrasado | guarda `resolved` antes de chamar (§4.4.3) |
| Exceção não tratada no provedor | **ligação repetida sem limite** (Celery `max_retries=None`) | `except Exception → FailedToMakeCall` (§4.2) |
| Failover de originate com timeout de leitura | 2 ligações para o mesmo alerta | trocar de nó só em erro de conexão (§4.2) |
| Tempestade de alertas | N ligações para a mesma pessoa; GoIP de 1 canal congestiona | `GROUP_COUNT` por número, filtrar o que liga (§5.8) |
| Operadora bloqueia o chip (SIM box) ou desliga o 2G | caminho de ligação morre em silêncio | operadoras diferentes por POP, monitoramento (§4.9), plano B (SIP trunk de operadora fixa) |
| Celular em Não Perturbe | toca sem som | contato liberado (§5.5) |
| `GRAFANA_CLOUD_NOTIFICATIONS_ENABLED` no padrão `True` | ligação vai para a Grafana Cloud, não para o Asterisk | pôr `False` na implantação (§1.2) |
| `PHONE_PROVIDER` com nome errado | cai no Twilio em silêncio (`phone_provider.py:185-186`) | checar `env_status` na UI após a implantação |
| Limite diário de 200 ligações + SMS por usuário [medido: `settings/base.py:87`] | numa noite ruim a ligação para de sair às cegas | subir `PHONE_NOTIFICATIONS_LIMIT` |
| Conflito com o upstream | merge doloroso | manter o core intocado onde der (prompt fixo em vez de mudar o template) |

---

## 7. O que este desenho NÃO verificou

- **Nada foi executado:** nem testes do engine, nem `makemigrations`, nem um OnCall de pé. As linhas de
  código citadas foram lidas, não exercitadas.
- **Asterisk:**
  - os esboços de dialplan e de chamada ARI **não foram rodados**;
  - não confirmei: a propagação de variáveis `__` entre `Local;1` e `Local;2`; o DTMF atravessando canal
    `Local`; o barge-in do `Read`; o retorno do `CURL()` em erro HTTP; o comportamento de
    `GROUP_COUNT` com originate;
  - a extensão `failed` e o `Reason` do AMI vieram de issues e relatos via busca, não da fonte da versão
    que for usada.
- **GoIP:** nomes dos menus de DTMF, supervisão de atendimento, número de canais do modelo, SNMP ou API
  de status e envio de SMS. Nada disso foi visto num equipamento real.
- **Operadoras:** bloqueio de SIM box, temporizador de desvio para a caixa postal, regeneração de DTMF
  entre operadoras e em VoLTE, troca de bina, data do desligamento do 2G. Tudo [hipótese] ou fonte
  secundária.
- **OnCall fora do `engine/apps` lido:**
  - não li o frontend (`grafana-plugin`), então não sei se ele trata `flags` de um provedor
    desconhecido sem quebrar;
  - não li o app mobile, nem `helm/`/`docker-compose` (como as variáveis chegam ao container), nem os
    receptores do `user_notification_action_triggered_signal`;
  - não vi se o middleware de modo de manutenção bloqueia o callback;
  - não verifiquei a ordem de grandeza da latência entre o `CURL` e a resposta do OnCall.
- **TTS:** só conferi a lista de vozes pt_BR do Piper (4 vozes). Não ouvi a qualidade, não medi o tempo
  de síntese e não testei a conversão para 8 kHz.

### Fontes externas consultadas (documentação pública)

- [ARI — Channels REST API (POST /channels)](https://docs.asterisk.org/Latest_API/API_Documentation/Asterisk_REST_Interface/Channels_REST_API/)
- [Dialplan function CURLOPT](https://docs.asterisk.org/Latest_API/API_Documentation/Dialplan_Functions/CURLOPT/)
- [Dialplan application Read](https://docs.asterisk.org/Latest_API/API_Documentation/Dialplan_Applications/Read/)
- [res_pjsip — dtmf_mode](https://docs.asterisk.org/Latest_API/API_Documentation/Module_Configuration/res_pjsip/)
- [ASTERISK-26115 — AMI Originate ignora a extensão failed](https://issues-archive.asterisk.org/ASTERISK-26115) e [correção de 2017](https://lists.digium.com/pipermail/asterisk-code-review/2017-February/039240.html)
- [3CX community — DTMF issue (GoIP4, 1 SIM sem DTMF)](https://www.3cx.com/community/threads/dtmf-issue.107537/)
- [Piper — VOICES.md](https://raw.githubusercontent.com/rhasspy/piper/master/VOICES.md)
- [Tecnoblog — Vivo quer desligar a rede 2G “o quanto antes”](https://tecnoblog.net/noticias/vivo-quer-desligar-a-rede-2g-o-quanto-antes/)
- [Teletime — desligamento do 2G e 3G (Gristec, 2024)](https://teletime.com.br/09/12/2024/desligamento-do-2g-e-3g-ha-motivo-para-tensao-mas-nao-para-panico-diz-gristec/)
