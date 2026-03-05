-- Trial (2 dias grátis) e assinatura (30 dias após pagamento)
alter table public.user_config add column if not exists trial_ends_at timestamptz;
alter table public.user_config add column if not exists subscription_ends_at timestamptz;

comment on column public.user_config.trial_ends_at is 'Fim do período de teste (2 dias a partir do primeiro acesso)';
comment on column public.user_config.subscription_ends_at is 'Fim do acesso pago (30 dias após confirmação de 100 USDC)';
