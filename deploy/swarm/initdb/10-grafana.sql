-- Banco do Grafana de teste no mesmo Postgres do OnCall (como faz dev/.env.postgres.dev).
-- Só roda na primeira inicialização do volume.
CREATE DATABASE grafana OWNER oncall;
