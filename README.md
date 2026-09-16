# ouros analytics database

Camada analítica PostgreSQL derivada do banco transacional de produção. O
repositório mantém estruturas voltadas para leitura, dashboards e métricas;
ele não replica o schema operacional.

## Estrutura

- `sql/`: schema analítico versionado
- `scripts/apply_sql.py`: orquestrador local
- `config.yaml`: ordem de execucao e configuracao
- `.env.example`: variaveis sensiveis

## Modelo analítico

O arquivo `sql/analytics_schema.sql` cria o schema `analytics` com dimensões de
empresas e fazendas, fatos de lotes, consumo mensal, pagamentos, metas e
feedback de dicas, além da view `analytics.v_farm_dashboard`.

### Origem e tratamento

Foram utilizados `enterprises`, `addresses`, `farms`, `lots`,
`water_registries`, `energy_registries`, `plans`, `enterprise_plans`,
`payments`, `individual_goals`, `state_goals`, `regions_goals`,
`farm_goals`, `state_goal_regions`, `tips`, `categories`,
`tip_categories` e `reviews`.

Os joins empresa-endereço-fazenda, empresa-plano-pagamento, fazenda-lote,
fazenda-consumo e dica-categoria-avaliação são preparados no modelo analítico.
Consumo é agregado por fazenda e mês; `losts` vira `lost_chickens`; tipos e
status devem ser carregados como `lower(btrim(valor))`; percentuais, capacidade
disponível, aves sobreviventes, custo unitário e CGI são calculados no banco.
Água é padronizada em m³ e energia em kWh.

Foram ignorados `farm_owners`, `company_employees` e `adms` por conterem
credenciais, documentos, telefones e e-mails; `password`, `foto_url`, número
da rua, CEP, comentários e textos de dicas também não entram no analytics.
`chicken_left` e as tabelas de log foram ignoradas por duplicarem eventos
operacionais sem uma dimensão temporal suficiente para análise.

O carregamento deve inserir os dados já tratados diretamente nas tabelas
`analytics.*`; a aplicação do schema não copia tabelas de produção nem expõe
dados pessoais.

## Acesso de serviços

O role `analytics_ro` é criado para APIs, dashboards e ferramentas de análise.
Ele possui apenas `USAGE` no schema `analytics` e `SELECT` nas tabelas e views,
com transações somente leitura e sem privilégios administrativos. A senha não
é versionada e deve ser configurada no ambiente de execução pelo gerenciador
de secrets.

## Sincronização incremental

O script `scripts/sync_analytics.py` lê somente registros com `updated_at` no
intervalo entre o último watermark e o início da execução. Ele usa o role
`analytics_sync_ro` na origem e uma credencial de escrita separada no destino,
aplica joins/agregações/normalizações e grava com UPSERT. O watermark fica em
`analytics.sync_state` e só é atualizado no mesmo commit dos dados; qualquer
falha faz rollback e mantém o `last_sync` anterior.

Configure `PRODUCTION_DATABASE_URL` e `ANALYTICS_SYNC_DATABASE_URL` no ambiente
e execute:

```bash
python scripts/sync_analytics.py
```

Para cron, por exemplo, use `*/5 * * * * cd /caminho/ouros-analytics-database &&
python scripts/sync_analytics.py >> /var/log/ouros-analytics-sync.log 2>&1`.

## Uso

```bash
pip install -r requirements.txt
python scripts/apply_sql.py
```
