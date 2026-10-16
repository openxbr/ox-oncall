#!/usr/bin/env python3
"""Configura a PoC do OnCall (stack oncall-teste). Roda NO manager do Swarm, onde estão os segredos:

    ssh root@<manager> python3 - <comando> [args] < deploy/swarm/configurar_poc.py

O endereço da stack vem de ONCALL_HOST no stack.env (ver stack.env.example).

Os segredos (senha do admin, senhas dos usuários, token da API pública) são lidos e gravados
em /mnt2/oncall-teste/stack/ (arquivos 600) e não saem impressos.

Comandos:
  install                      liga o plugin ao engine e mostra o status
  usuarios                     cria no Grafana os 2 plantonistas da PoC e sincroniza o OnCall
  token                        cria o token da API pública do OnCall (api-token, 600)
  zabbix                       cria a integração "Zabbix (TESTE)" e imprime a URL dela
  escala                       escala diária A/B + escalation chain por tempo + rota padrão
  atualizar-escala             põe os USERS atuais no turno e na chain, remove os RETIRED
  regras                       regra pessoal "notificar pelo Telegram" para os 2 plantonistas
  log <alert_group_id>         linha do tempo do alert group (escalonamento, ack, resolve)
  telegram-codigo              código de verificação do canal (ChatOps -> Telegram)
  org-nome <nome>              troca o nome da organização (padrão OSS: "Self-Hosted Organization")
  telegram-usuario <login>     código para o plantonista ligar a conta pessoal ao bot
  telegram-rota <canal_id>     liga a rota padrão da integração ao canal do Telegram
  telegram-rota off            só DM: a rota para de publicar no canal (alerta completo na DM)
  status                       resumo do que existe
  get <caminho>                GET na API pública (ex.: alert_groups/), só leitura
  iget <caminho>               GET na API interna via plugin (ex.: telegram_channels/), só leitura
"""
import base64
import json
import os
import secrets
import sys
import urllib.error
import urllib.request

STACK = "/mnt2/oncall-teste/stack"


def _oncall_host():
    if os.environ.get("ONCALL_HOST"):
        return os.environ["ONCALL_HOST"]
    for line in open(os.path.join(STACK, "stack.env")):
        if line.startswith("ONCALL_HOST="):
            return line.split("=", 1)[1].strip()
    raise SystemExit("ONCALL_HOST não definido (stack.env)")


HOST = _oncall_host()
GRAFANA = f"http://{HOST}:13000"
ENGINE = f"http://{HOST}:18080"
PLUGIN = "/api/plugins/grafana-oncall-app/resources"
TZ = "America/Sao_Paulo"

# Os 2 plantonistas da PoC vêm de plantonistas.json, ao lado dos segredos (fora do git):
#   {"users": [{"login": "...", "name": "...", "email": "...", "role": "Admin"}, {...}],
#    "retired": ["login-que-saiu"]}
# Sem o arquivo, vale o exemplo abaixo.
_PLANT = os.path.join(STACK, "plantonistas.json")
_cfg = json.load(open(_PLANT)) if os.path.exists(_PLANT) else {
    "users": [
        {"login": "pessoa-a", "name": "Pessoa A", "email": "pessoa-a@example.invalid", "role": "Admin"},
        {"login": "pessoa-b", "name": "Pessoa B", "email": "pessoa-b@example.invalid", "role": "Editor"},
    ],
    "retired": [],
}
USERS = _cfg["users"]
RETIRED = _cfg.get("retired", [])
SCHEDULE = "Plantão NOC (TESTE)"
CHAIN = "Escalonamento NOC (TESTE)"
INTEGRATION = "Zabbix (TESTE)"
WAIT_SECONDS = 120


def envfile(name):
    out = {}
    path = os.path.join(STACK, name)
    if os.path.exists(path):
        for line in open(path):
            if "=" in line and not line.startswith("#"):
                k, v = line.rstrip("\n").split("=", 1)
                out[k] = v
    return out


def save_secret(name, text):
    path = os.path.join(STACK, name)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)


def http(method, url, body=None, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, raw.decode(errors="replace")[:300]


def admin_auth():
    pw = envfile("grafana.env")["GF_SECURITY_ADMIN_PASSWORD"]
    return {"Authorization": "Basic " + base64.b64encode(f"admin:{pw}".encode()).decode()}


def grafana(method, path, body=None):
    return http(method, GRAFANA + path, body, admin_auth())


def public(method, path, body=None):
    tok = open(os.path.join(STACK, "api-token")).read().strip()
    return http(method, ENGINE + "/api/v1/" + path, body, {"Authorization": tok})


def must(res, what):
    code, body = res
    if code >= 300:
        raise SystemExit(f"{what}: HTTP {code} {body}")
    return body


def cmd_install():
    print("install:", grafana("POST", PLUGIN + "/plugin/install"))
    print("status:", grafana("GET", PLUGIN + "/plugin/status"))


def cmd_usuarios():
    pws = envfile("usuarios.env")
    for u in USERS:
        code, found = grafana("GET", f"/api/users/lookup?loginOrEmail={u['login']}")
        if code == 200:
            uid = found["id"]
            print(f"{u['login']}: já existe (id {uid})")
        else:
            pw = pws.get(u["login"]) or secrets.token_urlsafe(18)
            pws[u["login"]] = pw
            uid = must(grafana("POST", "/api/admin/users", {"name": u["name"], "email": u["email"], "login": u["login"], "password": pw}), u["login"])["id"]
            print(f"{u['login']}: criado (id {uid})")
        must(grafana("PATCH", f"/api/org/users/{uid}", {"role": u["role"]}), f"role {u['login']}")
    save_secret("usuarios.env", "".join(f"{k}={v}\n" for k, v in pws.items()))
    # Sem wait/force o plugin manda o que tem em cache e o usuário recém-criado não chega.
    print("sync:", grafana("POST", PLUGIN + "/plugin/sync?wait=true&force=true"))


def cmd_token():
    path = os.path.join(STACK, "api-token")
    if os.path.exists(path):
        print("api-token já existe; nada feito")
        return
    body = must(grafana("POST", PLUGIN + "/tokens/", {"name": "poc-claude"}), "token")
    save_secret("api-token", body["token"] + "\n")
    print("token criado:", {k: body[k] for k in ("id", "name", "created_at")})


def find(path, name, key="name"):
    body = must(public("GET", path), path)
    return next((x for x in body.get("results", []) if x.get(key) == name), None)


def cmd_zabbix():
    integ = find("integrations/", INTEGRATION)
    if not integ:
        integ = must(public("POST", "integrations/", {"type": "zabbix", "name": INTEGRATION}), "integration")
        print("integração criada:", integ["id"])
    print("URL:", integ["link"])


def user_ids():
    body = must(public("GET", "users/"), "users")
    by_login = {u["username"]: u["id"] for u in body.get("results", [])}
    missing = [u["login"] for u in USERS if u["login"] not in by_login]
    if missing:
        raise SystemExit(f"usuários ainda não sincronizados no OnCall: {missing} (rode 'usuarios' e espere o sync)")
    return [by_login[u["login"]] for u in USERS]


def cmd_escala():
    a, b = user_ids()
    sched = find("schedules/", SCHEDULE)
    if not sched:
        shift = must(public("POST", "on_call_shifts/", {
            "name": "Rodízio diário A/B (TESTE)", "type": "rolling_users", "time_zone": TZ,
            "start": "2026-10-15T08:00:00", "duration": 86400, "frequency": "daily", "interval": 1,
            "rolling_users": [[a], [b]], "start_rotation_from_user_index": 0,
        }), "shift")
        sched = must(public("POST", "schedules/", {
            "name": SCHEDULE, "type": "web", "time_zone": TZ, "shifts": [shift["id"]],
        }), "schedule")
        print("escala criada:", sched["id"], "turno:", shift["id"])
    chain = find("escalation_chains/", CHAIN)
    if not chain:
        chain = must(public("POST", "escalation_chains/", {"name": CHAIN}), "chain")
        steps = [
            {"type": "notify_on_call_from_schedule", "notify_on_call_from_schedule": sched["id"]},
            {"type": "wait", "duration": WAIT_SECONDS},
            {"type": "notify_persons", "persons_to_notify": [a, b]},
        ]
        for pos, st in enumerate(steps):
            must(public("POST", "escalation_policies/", {"escalation_chain_id": chain["id"], "position": pos, **st}), f"passo {pos}")
        print("chain criada:", chain["id"], "com", len(steps), "passos")
    integ = find("integrations/", INTEGRATION)
    if integ:
        route = integ["default_route"]
        print("rota padrão:", must(public("PUT", f"routes/{route['id']}/", {"escalation_chain_id": chain["id"]}), "route"))
    print("de plantão agora:", must(public("GET", f"schedules/{sched['id']}/"), "sched").get("on_call_now"))


def cmd_atualizar_escala():
    # Põe os USERS atuais no turno da escala e no passo "notify_persons" da chain, e tira do
    # Grafana os usuários de RETIRED (o OnCall os remove no sync seguinte).
    a, b = user_ids()
    sched = find("schedules/", SCHEDULE)
    for sid in must(public("GET", f"schedules/{sched['id']}/"), "sched")["shifts"]:
        print("turno:", must(public("PUT", f"on_call_shifts/{sid}/", {"rolling_users": [[a], [b]]}), "shift")["rolling_users"])
    chain = find("escalation_chains/", CHAIN)
    for pol in must(public("GET", f"escalation_policies/?escalation_chain_id={chain['id']}"), "pols")["results"]:
        if pol["type"] == "notify_persons":
            print("passo", pol["position"], ":", must(public("PUT", f"escalation_policies/{pol['id']}/", {"persons_to_notify": [a, b]}), "pol")["persons_to_notify"])
    for login in RETIRED:
        code, found = grafana("GET", f"/api/users/lookup?loginOrEmail={login}")
        if code == 200:
            print("removido do Grafana:", login, grafana("DELETE", f"/api/admin/users/{found['id']}"))
    print("sync:", grafana("POST", PLUGIN + "/plugin/sync?wait=true&force=true"))


def cmd_regras():
    # Usuário sincronizado do Grafana chega sem regra pessoal: a chain notificaria ninguém.
    rules = must(public("GET", "personal_notification_rules/"), "rules").get("results", [])
    for uid in user_ids():
        for important in (False, True):
            if any(r["user_id"] == uid and r.get("important") == important for r in rules):
                print(uid, "important" if important else "default", ": já tem regra")
                continue
            r = must(public("POST", "personal_notification_rules/", {
                "user_id": uid, "position": 0, "type": "notify_by_telegram", "important": important}), "rule")
            print(uid, "important" if important else "default", ":", r["id"], r["type"])


def cmd_log(ag):
    body = must(grafana("GET", PLUGIN + f"/alertgroups/{ag}/"), "alertgroup")
    for rec in body.get("render_after_resolve_report_json", []):
        print(rec.get("created_at"), "|", rec.get("type"), "|", (rec.get("action") or "")[:160])


def cmd_org_nome(nome):
    # O OSS cria a organização com SELF_HOSTED_ORG_TITLE ("Self-Hosted Organization") e o sync
    # nunca mais mexe no nome; é ele que aparece em "This channel is now linked to organization …".
    body = must(grafana("PUT", PLUGIN + "/current_team", {"name": nome}), "current_team")
    print("organização:", body.get("name"))


def cmd_telegram_codigo():
    # Código para postar no CANAL (ChatOps -> Telegram); o bot confirma no grupo de discussão.
    # É o endpoint que o plugin usa para Telegram; o get_channel_verification_code genérico
    # exige ?backend= e não atende o Telegram.
    print(must(grafana("GET", PLUGIN + "/current_team/get_telegram_verification_code/"), "codigo canal"))


def cmd_telegram_usuario(login):
    # Código para o plantonista mandar ao bot no privado: liga a conta dele ao usuário do OnCall.
    uid = dict(zip([u["login"] for u in USERS], user_ids()))[login]
    print(must(grafana("GET", PLUGIN + f"/users/{uid}/get_telegram_verification_code/"), "codigo usuario"))


def cmd_telegram_rota(canal):
    # "off" = só DM: a rota para de publicar no canal, e a notificação pessoal deixa de ser um
    # link para o post e passa a levar o alerta inteiro, com os botões e o log
    # (apps/telegram/models/connectors/personal.py, notify()).
    integ = find("integrations/", INTEGRATION)
    route = integ["default_route"]
    if canal == "off":
        body = {"telegram": {"id": (route.get("telegram") or {}).get("id"), "enabled": False}}
    else:
        body = {"telegram": {"id": canal, "enabled": True}}
    print(must(public("PUT", f"routes/{route['id']}/", body), "route")["telegram"])


def cmd_status():
    print("plugin:", grafana("GET", PLUGIN + "/plugin/status"))
    if os.path.exists(os.path.join(STACK, "api-token")):
        for p in ("users/", "integrations/", "schedules/", "escalation_chains/", "routes/"):
            code, body = public("GET", p)
            names = [x.get("name") or x.get("username") or x.get("id") for x in (body or {}).get("results", [])] if code == 200 else body
            print(p, code, names)


def main():
    cmds = {"install": cmd_install, "usuarios": cmd_usuarios, "token": cmd_token, "zabbix": cmd_zabbix,
            "escala": cmd_escala, "atualizar-escala": cmd_atualizar_escala, "regras": cmd_regras, "telegram-codigo": cmd_telegram_codigo,
            "status": cmd_status}
    if len(sys.argv) >= 3 and sys.argv[1] == "telegram-rota":
        return cmd_telegram_rota(sys.argv[2])
    if len(sys.argv) == 3 and sys.argv[1] == "org-nome":
        return cmd_org_nome(sys.argv[2])
    if len(sys.argv) == 3 and sys.argv[1] == "telegram-usuario":
        return cmd_telegram_usuario(sys.argv[2])
    if len(sys.argv) == 3 and sys.argv[1] == "log":
        return cmd_log(sys.argv[2])
    if len(sys.argv) == 3 and sys.argv[1] == "iget":  # leitura crua da API interna (via plugin)
        return print(json.dumps(must(grafana("GET", PLUGIN + "/" + sys.argv[2]), sys.argv[2]), indent=1, ensure_ascii=False))
    if len(sys.argv) == 3 and sys.argv[1] == "get":  # leitura crua da API pública
        return print(json.dumps(must(public("GET", sys.argv[2]), sys.argv[2]), indent=1, ensure_ascii=False))
    if len(sys.argv) != 2 or sys.argv[1] not in cmds:
        raise SystemExit(__doc__)
    cmds[sys.argv[1]]()


if __name__ == "__main__":
    main()
