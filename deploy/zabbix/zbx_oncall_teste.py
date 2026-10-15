#!/usr/bin/env python3
"""Objetos de TESTE do Zabbix para a PoC do OnCall. Dry-run por padrão; grava com --apply.

Só cria objetos novos, todos com nome próprio, e nunca altera objeto existente:

  base         host group OX-TESTE-ONCALL, host OX-ONCALL-TESTE, 2 itens trapper e
               2 triggers Warning (a action de produção "Trigger via Telegram" pega
               severidade >= Average, então estes não chegam aos grupos de produção)
  notificacao  media type webhook "Grafana OnCall (TESTE)", user group "OnCall TESTE",
               usuário oncall-teste (mídia = URL da integração) e a action restrita ao host
  disparar     manda 1 (problema) ou 0 (OK) a um item via history.push
  rollback     apaga tudo o que este script cria, na ordem inversa

Token: ~/.config/openx/zabbix-token (ou ZABBIX_TOKEN_FILE). URL: ZABBIX_URL.
"""
import argparse
import json
import os
import secrets
import sys
import urllib.request

URL = os.environ.get("ZABBIX_URL", "https://zabbix.openx.com.br/api_jsonrpc.php")
TOKEN_FILE = os.environ.get("ZABBIX_TOKEN_FILE", os.path.expanduser("~/.config/openx/zabbix-token"))
HERE = os.path.dirname(os.path.abspath(__file__))

GROUP = "OX-TESTE-ONCALL"
HOST = "OX-ONCALL-TESTE"
ITEMS = {"a": "oncall.teste[a]", "b": "oncall.teste[b]"}
MEDIATYPE = "Grafana OnCall (TESTE)"
USERGROUP = "OnCall TESTE"
USER = "oncall-teste"
ACTION = "OnCall TESTE (só OX-ONCALL-TESTE)"
SEV_WARNING = 2


def api(method, params):
    token = open(TOKEN_FILE).read().strip()
    body = json.dumps({"jsonrpc": "2.0", "method": method, "params": params, "id": 1}).encode()
    req = urllib.request.Request(URL, body, {"Content-Type": "application/json-rpc", "Authorization": f"Bearer {token}"})
    out = json.load(urllib.request.urlopen(req, timeout=30))
    if "error" in out:
        raise SystemExit(f"{method}: {out['error']}")
    return out["result"]


def write(apply, method, params):
    if not apply:
        print(f"[dry-run] {method} {json.dumps(params, ensure_ascii=False)[:300]}")
        return None
    res = api(method, params)
    print(f"[apply] {method} -> {res}")
    return res


def one(method, params, what):
    res = api(method, params)
    return res[0] if res else None


def cmd_base(a):
    if one("hostgroup.get", {"filter": {"name": [GROUP]}}, "group") or one("host.get", {"filter": {"host": [HOST]}}, "host"):
        raise SystemExit(f"{GROUP} ou {HOST} já existe; nada feito")
    res = write(a.apply, "hostgroup.create", {"name": GROUP})
    gid = res["groupids"][0] if res else "<groupid>"
    res = write(a.apply, "host.create", {
        "host": HOST,
        "groups": [{"groupid": gid}],
        "description": "TESTE da PoC do Grafana OnCall. Não monitora nada: itens trapper "
                       "alimentados por history.push (deploy/zabbix/zbx_oncall_teste.py no ox-oncall).",
        "tags": [{"tag": "oncall", "value": "teste"}],
    })
    hid = res["hostids"][0] if res else "<hostid>"
    for k, key in ITEMS.items():
        write(a.apply, "item.create", {
            "hostid": hid, "name": f"OnCall teste {k.upper()}", "key_": key,
            "type": 2, "value_type": 3, "history": "7d", "trends": "0",
        })
        write(a.apply, "trigger.create", {
            "description": f"OnCall TESTE {k.upper()}",
            "expression": f"last(/{HOST}/{key})=1",
            "priority": SEV_WARNING,
            "comments": "Trigger de TESTE da PoC do OnCall. Severidade Warning de propósito.",
        })


def cmd_notificacao(a):
    for m, f in (("mediatype.get", {"name": [MEDIATYPE]}), ("usergroup.get", {"name": [USERGROUP]}),
                 ("user.get", {"username": [USER]}), ("action.get", {"name": [ACTION]})):
        if one(m, {"filter": f}, m):
            raise SystemExit(f"{m} {f} já existe; nada feito")
    host = one("host.get", {"filter": {"host": [HOST]}, "output": ["hostid"]}, "host")
    group = one("hostgroup.get", {"filter": {"name": [GROUP]}, "output": ["groupid"]}, "group")
    if not host or not group:
        raise SystemExit("rode 'base' antes")
    role = one("role.get", {"filter": {"name": ["User role"]}, "output": ["roleid"]}, "role")
    script = open(os.path.join(HERE, "oncall_webhook.js")).read()
    params = {
        "URL": "{ALERT.SENDTO}", "SUBJECT": "{ALERT.SUBJECT}", "MESSAGE": "{ALERT.MESSAGE}",
        "EVENT_ID": "{EVENT.ID}", "EVENT_VALUE": "{EVENT.VALUE}", "EVENT_UPDATE_STATUS": "{EVENT.UPDATE.STATUS}",
        "EVENT_SEVERITY": "{EVENT.SEVERITY}", "HOST": "{HOST.HOST}", "TRIGGER_ID": "{TRIGGER.ID}",
        "ZABBIX_URL": "https://zabbix.openx.com.br",
    }
    msg = "Host: {HOST.NAME}\nSeveridade: {EVENT.SEVERITY}\nInício: {EVENT.DATE} {EVENT.TIME}\nEvento: {EVENT.ID}"
    res = write(a.apply, "mediatype.create", {
        "name": MEDIATYPE, "type": 4, "status": 0, "script": script, "timeout": "10s",
        "maxattempts": 3, "attempt_interval": "10s",
        "parameters": [{"name": k, "value": v} for k, v in params.items()],
        "description": "TESTE da PoC do OnCall. Fonte: deploy/zabbix/oncall_webhook.js no ox-oncall.",
        "message_templates": [
            {"eventsource": 0, "recovery": 0, "subject": "Problem: {EVENT.NAME}", "message": msg},
            {"eventsource": 0, "recovery": 1, "subject": "Resolved: {EVENT.NAME}",
             "message": msg + "\nResolvido: {EVENT.RECOVERY.DATE} {EVENT.RECOVERY.TIME}"},
            {"eventsource": 0, "recovery": 2, "subject": "Updated: {EVENT.NAME}",
             "message": "{USER.FULLNAME} {EVENT.UPDATE.ACTION}\n{EVENT.UPDATE.MESSAGE}\n" + msg},
        ],
    })
    mtid = res["mediatypeids"][0] if res else "<mediatypeid>"
    res = write(a.apply, "usergroup.create", {
        "name": USERGROUP, "gui_access": 3, "users_status": 0,
        "hostgroup_rights": [{"id": group["groupid"], "permission": 2}],
    })
    ugid = res["usrgrpids"][0] if res else "<usrgrpid>"
    res = write(a.apply, "user.create", {
        "username": USER, "name": "OnCall", "surname": "TESTE", "passwd": secrets.token_urlsafe(24),
        "roleid": role["roleid"], "usrgrps": [{"usrgrpid": ugid}],
        "medias": [{"mediatypeid": mtid, "sendto": a.url, "active": 0, "severity": 63, "period": "1-7,00:00-24:00"}],
    })
    uid = res["userids"][0] if res else "<userid>"
    op = {"operationtype": 0, "opmessage": {"default_msg": 1, "mediatypeid": mtid}, "opmessage_usr": [{"userid": uid}]}
    write(a.apply, "action.create", {
        "name": ACTION, "eventsource": 0, "status": 0, "esc_period": "1h",
        "filter": {"evaltype": 0, "conditions": [{"conditiontype": 1, "operator": 0, "value": host["hostid"]}]},
        "operations": [op], "recovery_operations": [op], "update_operations": [op],
    })


def cmd_disparar(a):
    write(a.apply, "history.push", [{"host": HOST, "key": ITEMS[a.item], "value": a.valor}])


def cmd_rollback(a):
    act = one("action.get", {"filter": {"name": [ACTION]}, "output": ["actionid"]}, "a")
    usr = one("user.get", {"filter": {"username": [USER]}, "output": ["userid"]}, "u")
    ug = one("usergroup.get", {"filter": {"name": [USERGROUP]}, "output": ["usrgrpid"]}, "ug")
    mt = one("mediatype.get", {"filter": {"name": [MEDIATYPE]}, "output": ["mediatypeid"]}, "mt")
    h = one("host.get", {"filter": {"host": [HOST]}, "output": ["hostid"]}, "h")
    g = one("hostgroup.get", {"filter": {"name": [GROUP]}, "output": ["groupid"]}, "g")
    for obj, method, key in ((act, "action.delete", "actionid"), (usr, "user.delete", "userid"),
                             (ug, "usergroup.delete", "usrgrpid"), (mt, "mediatype.delete", "mediatypeid"),
                             (h, "host.delete", "hostid"), (g, "hostgroup.delete", "groupid")):
        if obj:
            write(a.apply, method, [obj[key]])
        else:
            print(f"(não existe) {method}")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--apply", action="store_true", help="grava de verdade (padrão: dry-run)")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("base")
    n = sub.add_parser("notificacao")
    n.add_argument("--url", required=True, help="URL da integração Zabbix no OnCall")
    d = sub.add_parser("disparar")
    d.add_argument("item", choices=sorted(ITEMS))
    d.add_argument("valor", choices=["0", "1"])
    sub.add_parser("rollback")
    a = p.parse_args()
    {"base": cmd_base, "notificacao": cmd_notificacao, "disparar": cmd_disparar, "rollback": cmd_rollback}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
