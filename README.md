# Ouros Analytics · MongoDB Atlas

Camada analítica do Ouros baseada **exclusivamente em MongoDB Atlas**. O PostgreSQL
Neon `segundo_prod` continua sendo a **origem transacional, somente leitura**.
Este repositório **não gerencia, migra ou provisiona** um PostgreSQL analítico
no homelab.

## Arquitetura

```text
Neon PostgreSQL (segundo_prod, analytics_sync_ro)
         | TLS verificado, SELECT, snapshot consistente
         v
GitHub Actions (MongoDB Analytics Sync, 17/47 min de cada hora UTC)
         | reconciliação completa / IDs determinísticos
         v
MongoDB Atlas (ouros_analytics_poc*)
         | documentos por dimensão/fato, mês e buckets de métricas
         v
Consumidores MongoDB (integração de telemetry em evolução)
```

O nome `ouros_analytics_poc` e os prefixos de coleção `poc_*` foram
**mantidos por compatibilidade** com os dados sincronizados e auditados.
Renomeá-los requer migração e validação separadas.

## Componentes

- `scripts/source_queries.py`: contrato SQL **de leitura** do banco de produção.
  Não cria tabelas, não insere dados nem executa migrations.
- `scripts/sync_mongodb.py`: extrai dados operacionais, reconcilia documentos
  em lote, atualiza checkpoints e trata exclusões/correções da origem.
- `scripts/audit_mongodb_poc.py`: verifica idempotência, paridade com a origem
  e consumo de espaço. Auditoria manual, não faz escrita no PostgreSQL.
- `.github/workflows/mongodb-sync-poc.yml`: único workflow de sincronização
  analítica, executado pelo GitHub Actions.
- `.github/workflows/ci-cd.yml`: testes da camada Mongo, CodeQL e SonarCloud.

### Coleções no Atlas

| Coleção | Conteúdo |
| --- | --- |
| `poc_analytics_rows` | Empresas, fazendas, lotes, pagamentos, metas, feedback de dicas |
| `poc_farm_monthly` | Histórico de consumo agregado por fazenda e mês |
| `poc_metric_buckets` | Leituras de água e energia agrupadas por tipo/fazenda/mês |
| `poc_sync_state` | Watermark, status e contabilização da última carga |

## Sincronização automática

No GitHub, em **Settings → Secrets and variables → Actions**, configure:

| Tipo | Nome | Finalidade |
| --- | --- | --- |
| Secret | `PRODUCTION_DATABASE_URL` | URI somente leitura de `segundo_prod`, verificação TLS `verify-full` |
| Secret | `MONGODB_URI` | Usuário Atlas limitado ao banco analítico, com TLS validado |
| Variable | `MONGODB_DATABASE` | Opcional, por padrão `ouros_analytics_poc` |
| Variable | `MONGO_SYNC_ENABLED` | `true` habilita a carga agendada/manual |

A cada minuto **17 e 47 de cada hora UTC** o GitHub Actions executa os
testes e, quando `MONGO_SYNC_ENABLED=true`, executa o sync. Também há o
disparador manual `workflow_dispatch`. O evento `push` não inicia cargas de
dados; os testes de PR e push rodam no workflow CI/CD separado.

A sincronização não é CDC: o extrator faz **reconciliação completa** dos dados
em cada ciclo. Ela foi validada na POC, mas deve ser revisitada conforme o
volume crescer. Não há transação atômica entre várias coleções: o consumidor
deve validar `poc_sync_state.status == "success"` e a idade do checkpoint.

## Desenvolvimento

```bash
python -m pip install -r requirements.txt
python -m unittest discover -s tests -p 'test_*mongodb*.py' -v
```

Para executar o sync localmente, configure as três variáveis da tabela acima
(com `MONGO_SYNC_ENABLED` dispensável para execução local) e execute:

```bash
python -m scripts.sync_mongodb
```

Veja [docs/mongodb-sync.md](docs/mongodb-sync.md) para detalhes operacionais,
segurança, auditoria e procedimentos de desativação da camada anterior.

## Migração do PostgreSQL analítico do homelab

Este repositório deixa de versionar as migrations SQL, o sincronizador
PostgreSQL → PostgreSQL e o pipeline de deploy do homelab. **A mudança no
GitHub não apaga nem para** o banco, containers ou timers já existentes na
máquina. Para encerrar os recursos legados, é necessária uma ação operacional
separada, após conferir consumidores, backup e rollback.

O `ms-telemetry-dashboard-service` ainda possui provider PostgreSQL Analytics;
a migração desse consumidor para Atlas ocorre no repositório do serviço.
Até lá, os dashboards de negócio podem ficar indisponíveis. O provider
Prometheus e demais serviços do homelab permanecem fora do escopo.

**Nenhum DROP DATABASE, DROP SCHEMA, TRUNCATE, Docker volume remove ou comando
de desativação remota é executado por este repositório.**
