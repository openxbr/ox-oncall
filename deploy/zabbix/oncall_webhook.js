// Media type webhook do Zabbix 7.0 -> integração "Zabbix" do Grafana OnCall.
//
// O formato vem de engine/config_integrations/zabbix.py:
//   grouping_id       = payload.alert_uid   -> {EVENT.ID}, o mesmo no problema, no update e
//                                              na recuperação (é o que os webhooks stock
//                                              Opsgenie/PagerDuty usam como alias/dedup_key)
//   resolve_condition = payload.state == "ok" -> {EVENT.VALUE} = 0 (recuperação)
//
// Update operation (ack/comentário no Zabbix) vira mais um alerta no MESMO alert group.
// Update sobre evento já resolvido é descartado, como no webhook stock do Opsgenie.
//
// Parâmetros do media type:
//   URL={ALERT.SENDTO}  SUBJECT={ALERT.SUBJECT}  MESSAGE={ALERT.MESSAGE}
//   EVENT_ID={EVENT.ID}  EVENT_VALUE={EVENT.VALUE}  EVENT_UPDATE_STATUS={EVENT.UPDATE.STATUS}
//   EVENT_SEVERITY={EVENT.SEVERITY}  HOST={HOST.HOST}  TRIGGER_ID={TRIGGER.ID}
//   ZABBIX_URL=https://zabbix.openx.com.br

var p = JSON.parse(value);

if (!p.URL || p.URL.charAt(0) === '{') {
    throw 'URL vazia: preencher o "Send to" da mídia do usuário com a URL da integração';
}
if (p.EVENT_VALUE !== '0' && p.EVENT_VALUE !== '1') {
    throw 'EVENT_VALUE inesperado: ' + p.EVENT_VALUE;
}
if (p.EVENT_VALUE === '0' && p.EVENT_UPDATE_STATUS === '1') {
    return 'skip: update sobre evento resolvido';
}

var body = {
    alert_uid: p.EVENT_ID,
    title: p.SUBJECT,
    message: p.MESSAGE,
    state: p.EVENT_VALUE === '0' ? 'ok' : 'alerting',
    link_to_upstream_details: p.ZABBIX_URL + '/tr_events.php?triggerid=' + p.TRIGGER_ID + '&eventid=' + p.EVENT_ID,
    severity: p.EVENT_SEVERITY,
    host: p.HOST,
    is_update: p.EVENT_UPDATE_STATUS === '1'
};

var req = new HttpRequest();
req.addHeader('Content-Type: application/json');
var resp = req.post(p.URL, JSON.stringify(body));
var status = req.getStatus();

Zabbix.log(4, '[OnCall webhook] HTTP ' + status + ' alert_uid=' + p.EVENT_ID + ' state=' + body.state);

if (status < 200 || status >= 300) {
    throw 'OnCall respondeu HTTP ' + status + ': ' + String(resp).substring(0, 200);
}
return 'OK';
