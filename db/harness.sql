-- Harness findings DB (guide §8 run schema + guardrail events), the
-- dashboard's read-only role and the orchestrator's writer role. Idempotent;
-- setup-2060.sh provisions the roles and applies this schema.
-- Lives in the default `postgres` database; Langfuse has its own `langfuse` DB.
--
-- Orchestrator contract (what the dashboard reads):
--   runs.id            = LiteLLM metadata session_id on every call of the run
--   probes.interesting = glue's looks_interesting(obs) verdict
--   verdicts.evidence_ref must point at a replayable request/response pair;
--   confirmed + evidence + not duplicate = a finding.

set client_min_messages = warning;

create schema if not exists harness;

-- This schema is separate from the findings writer's broad table grants.
create schema if not exists harness_guardrails;

create table if not exists harness_guardrails.dispatch_policy (
  singleton        boolean primary key default true check (singleton),
  stopped          boolean not null default true,
  min_interval_ms  integer not null default 250 check (min_interval_ms > 0),
  last_dispatch_at timestamptz not null default '-infinity'
);

insert into harness_guardrails.dispatch_policy (singleton) values (true)
on conflict (singleton) do nothing;

-- Claim is an atomic, stop-aware reservation. Completion extends pacing from
-- the end of the request. Both writes survive runner disconnects.
create or replace function harness_guardrails.advance_dispatch(claim boolean)
returns boolean
language plpgsql
security definer
set search_path = pg_catalog
as $advance$
begin
  update harness_guardrails.dispatch_policy
     set last_dispatch_at = greatest(last_dispatch_at, clock_timestamp())
   where singleton
     and (not claim or (
       not stopped and last_dispatch_at + min_interval_ms * interval '1 millisecond' <= clock_timestamp()
     ));
  return found;
end;
$advance$;

revoke all on schema harness_guardrails from public;
revoke all on function harness_guardrails.advance_dispatch(boolean) from public;

create table if not exists harness.runs (
  id         text primary key,
  started_at timestamptz not null default now(),
  ended_at   timestamptz,
  target     text not null,
  program    text,
  status     text not null default 'running'
             check (status in ('running', 'done', 'failed', 'killed'))
);

create table if not exists harness.operations (
  id            bigserial primary key,
  run_id        text not null references harness.runs (id) on delete cascade,
  method        text not null,
  path          text not null,
  schema_source text,                         -- openapi | graphql | apk
  created_at    timestamptz not null default now()
);

create table if not exists harness.probes (
  id          bigserial primary key,
  op_id       bigint not null references harness.operations (id) on delete cascade,
  identity    text,
  tenant      text,
  role        text,
  intent      text,
  interesting boolean,                         -- null until glue has observed it
  created_at  timestamptz not null default now()
);

create table if not exists harness.exchanges (
  id            bigserial primary key,
  probe_id      bigint not null references harness.probes (id) on delete cascade,
  status_code   int,
  request_blob  text,                          -- artifact-store refs, not bodies
  response_blob text,
  created_at    timestamptz not null default now()
);

create table if not exists harness.verdicts (
  id           bigserial primary key,
  probe_id     bigint not null references harness.probes (id) on delete cascade,
  label        text not null check (label in ('confirmed', 'rejected', 'needs_review')),
  severity     text,
  is_duplicate boolean not null default false, -- Qdrant similarity hit
  evidence_ref text,                           -- replayable pair; null = no report
  trace_id     text,                           -- Langfuse trace of the judge call
  judged_at    timestamptz not null default now()
);

create table if not exists harness.guardrail_events (
  id     bigserial primary key,
  ts     timestamptz not null default now(),
  run_id text references harness.runs (id) on delete set null,
  kind   text not null check (kind in ('scope_denied', 'rate_limited', 'kill_switch')),
  host   text,
  detail text                                  -- kill_switch: 'engaged ...' | 'released'
);

create index if not exists runs_started_idx      on harness.runs (started_at);
create index if not exists operations_run_idx    on harness.operations (run_id);
create index if not exists operations_created_idx on harness.operations (created_at);
create index if not exists probes_op_idx         on harness.probes (op_id);
create index if not exists probes_created_idx    on harness.probes (created_at);
create index if not exists exchanges_probe_idx   on harness.exchanges (probe_id);
create index if not exists verdicts_probe_idx    on harness.verdicts (probe_id);
create index if not exists verdicts_judged_idx   on harness.verdicts (judged_at);
create index if not exists guardrail_ts_idx      on harness.guardrail_events (ts);

-- Dashboard role: SELECT on harness only, read-only sessions, 5 s statements.
-- No grants on the langfuse database's tables.
select 'create role dashboard_ro login' where not exists (select from pg_roles where rolname = 'dashboard_ro')
\gexec
alter role dashboard_ro password :'ro_password';
alter role dashboard_ro set default_transaction_read_only = on;
alter role dashboard_ro set statement_timeout = '5s';
grant usage on schema harness to dashboard_ro;
grant select on all tables in schema harness to dashboard_ro;
grant usage on schema harness_guardrails to dashboard_ro;
grant select (singleton, stopped) on harness_guardrails.dispatch_policy to dashboard_ro;
alter default privileges in schema harness grant select on tables to dashboard_ro;

-- Orchestrator role (HARNESS_DATABASE_URL on the M1): writes runs, never deletes,
-- no DDL, nothing outside schema harness. Only created when rw_password is passed.
\if :{?rw_password}
select 'create role harness_rw login' where not exists (select from pg_roles where rolname = 'harness_rw')
\gexec
alter role harness_rw password :'rw_password';
alter role harness_rw set statement_timeout = '30s';
grant usage on schema harness to harness_rw;
grant select, insert, update on all tables in schema harness to harness_rw;
grant usage, select on all sequences in schema harness to harness_rw;
grant usage on schema harness_guardrails to harness_rw;
grant select (singleton, stopped) on harness_guardrails.dispatch_policy to harness_rw;
grant execute on function harness_guardrails.advance_dispatch(boolean) to harness_rw;
alter default privileges in schema harness grant select, insert, update on tables to harness_rw;
alter default privileges in schema harness grant usage, select on sequences to harness_rw;
\endif
