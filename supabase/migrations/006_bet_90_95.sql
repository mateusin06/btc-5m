-- Aposta fixa (USD) para o modo 90-95 (últimos 10s, maior odd entre 90c e 95c)
alter table public.user_config add column if not exists bet_90_95 double precision;
