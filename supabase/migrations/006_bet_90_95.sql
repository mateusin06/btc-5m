-- Aposta fixa (USD) para o modo 90-95 (janela 20s–2s, spike/confiança, preço 80–95c)
alter table public.user_config add column if not exists bet_90_95 double precision;
